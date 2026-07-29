"""Create the IAM role, S3 Vectors store, knowledge bases, and data sources.

Three knowledge-base shapes are built here:

  VECTOR  (titan, nova) -- we own the vector store (S3 Vectors), the chunking
      strategy, and the parser (BEDROCK_FOUNDATION_MODEL).
  MANAGED (managed)     -- Bedrock Managed Knowledge Base. AWS owns the vector
      store, chunking, embeddings, and parsing (SMART_PARSING), and the data
      source is a MANAGED_KNOWLEDGE_BASE_CONNECTOR rather than a raw S3 source.

All of it is idempotent: re-running reuses whatever already exists.
"""

from __future__ import annotations

import json
import time

from . import config
from .aws import (
    account_id,
    client,
    get_state,
    is_error,
    model_arn,
    save_state,
    tag_list,
    tag_map,
)
from .config import Experiment
from .upload import CORPUS_PREFIX, bucket_name, ensure_bucket

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def _inline_policy(exp: Experiment, corpus_bucket: str) -> dict:
    acct = account_id()
    statements = [
        {
            "Sid": "InvokeModels",
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": [
                f"arn:aws:bedrock:{exp.region}::foundation-model/*",
                f"arn:aws:bedrock:{exp.region}:{acct}:inference-profile/*",
                # Inference profiles fan out to other regions; the profile grant
                # alone isn't enough without the underlying model in each.
                "arn:aws:bedrock:*::foundation-model/*",
            ],
        },
        {
            "Sid": "ReadCorpus",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:ListBucket"],
            "Resource": [
                f"arn:aws:s3:::{corpus_bucket}",
                f"arn:aws:s3:::{corpus_bucket}/*",
            ],
            "Condition": {"StringEquals": {"aws:ResourceAccount": acct}},
        },
    ]
    if exp.kb_type != "MANAGED":
        statements.append(
            {
                # Where the FM parser writes extracted figures and tables.
                "Sid": "WriteSupplementalData",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{exp.supplemental_bucket(acct)}",
                    f"arn:aws:s3:::{exp.supplemental_bucket(acct)}/*",
                ],
            }
        )
    if exp.uses_s3_vectors:
        statements.append(
            {
                "Sid": "S3Vectors",
                "Effect": "Allow",
                "Action": ["s3vectors:*"],
                "Resource": [
                    f"arn:aws:s3vectors:{exp.region}:{acct}:bucket/{exp.vector_bucket}",
                    f"arn:aws:s3vectors:{exp.region}:{acct}:bucket/{exp.vector_bucket}/*",
                ],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def ensure_role(exp: Experiment) -> str:
    """Create/update the KB service role. Returns its ARN."""
    iam = client("iam")  # IAM is global
    corpus_bucket = bucket_name(exp.region)
    name = exp.role_name
    try:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description=f"Bedrock KB role for {exp.label}",
            Tags=tag_list(),
        )["Role"]["Arn"]
        created = True
    except Exception as exc:
        if not is_error(exc, "EntityAlreadyExists"):
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        created = False
        # TagRole merges rather than replacing, so this is safe to repeat and
        # picks up roles created before tagging was added.
        iam.tag_role(RoleName=name, Tags=tag_list())

    iam.put_role_policy(
        RoleName=name,
        PolicyName="kb-access",
        PolicyDocument=json.dumps(_inline_policy(exp, corpus_bucket)),
    )
    # IAM is eventually consistent, and Bedrock validates the role's bucket
    # access inside CreateKnowledgeBase -- so wait after *any* policy write, not
    # just on creation, or a re-run races its own permission update.
    time.sleep(20 if created else 12)
    return arn


def ensure_vector_index(exp: Experiment) -> tuple[str, str]:
    """Create the S3 Vectors bucket + index. Returns (bucket_arn, index_arn)."""
    s3v = client("s3vectors", exp.region)
    acct = account_id()

    try:
        s3v.create_vector_bucket(vectorBucketName=exp.vector_bucket, tags=tag_map())
    except Exception as exc:
        if not is_error(exc, "ConflictException", "BucketAlreadyExists"):
            raise

    try:
        s3v.create_index(
            vectorBucketName=exp.vector_bucket,
            indexName=exp.vector_index,
            dataType=exp.embedding_data_type.lower(),
            dimension=exp.embedding_dims,
            distanceMetric=config.DISTANCE_METRIC,
            metadataConfiguration={
                # S3 Vectors allows only 2 KB of *filterable* metadata per
                # vector. Bedrock's own chunk-text and document-metadata keys
                # exceed that on their own, so they must be declared here or
                # every PutVectors call fails with "Filterable metadata must
                # have at most 2048 bytes".
                "nonFilterableMetadataKeys": config.NON_FILTERABLE_METADATA_KEYS
            },
            tags=tag_map(),
        )
    except Exception as exc:
        if not is_error(exc, "ConflictException", "IndexAlreadyExists"):
            raise

    bucket_arn = f"arn:aws:s3vectors:{exp.region}:{acct}:bucket/{exp.vector_bucket}"
    index_arn = f"{bucket_arn}/index/{exp.vector_index}"
    # Tag retroactively as well: both calls above are no-ops once the bucket and
    # index exist, so create-time tags never reach resources built earlier.
    for arn in (bucket_arn, index_arn):
        s3v.tag_resource(resourceArn=arn, tags=tag_map())
    return bucket_arn, index_arn


def _kb_config(exp: Experiment) -> dict:
    if exp.kb_type == "MANAGED":
        # embeddingModelType MANAGED = let AWS pick and manage the embedder.
        return {
            "type": "MANAGED",
            "managedKnowledgeBaseConfiguration": {"embeddingModelType": "MANAGED"},
        }
    return {
        "type": "VECTOR",
        "vectorKnowledgeBaseConfiguration": {
            "embeddingModelArn": model_arn(exp.embedding_model, exp.region),
            "embeddingModelConfiguration": {
                "bedrockEmbeddingModelConfiguration": {
                    "dimensions": exp.embedding_dims,
                    "embeddingDataType": exp.embedding_data_type,
                }
            },
            # Gives the FM parser somewhere to put extracted images/tables so
            # they can be returned in retrieval source attribution. Must be a
            # bucket root -- Bedrock rejects any sub-folder in this URI.
            "supplementalDataStorageConfiguration": {
                "storageLocations": [
                    {
                        "type": "S3",
                        "s3Location": {"uri": f"s3://{exp.supplemental_bucket(account_id())}"},
                    }
                ]
            },
        },
    }


def _find_kb(exp: Experiment) -> str | None:
    agent = client("bedrock-agent", exp.region)
    paginator = agent.get_paginator("list_knowledge_bases")
    for page in paginator.paginate():
        for summary in page.get("knowledgeBaseSummaries", []):
            if summary["name"] == exp.kb_name:
                return summary["knowledgeBaseId"]
    return None


def ensure_knowledge_base(exp: Experiment, role_arn: str) -> str:
    agent = client("bedrock-agent", exp.region)
    existing = _find_kb(exp)
    if existing:
        # Tag on re-run too, so a KB created before tagging was added gets them.
        kb_arn = agent.get_knowledge_base(knowledgeBaseId=existing)["knowledgeBase"][
            "knowledgeBaseArn"
        ]
        agent.tag_resource(resourceArn=kb_arn, tags=tag_map())
        return existing

    kwargs = {
        "name": exp.kb_name,
        "description": exp.label,
        "roleArn": role_arn,
        "knowledgeBaseConfiguration": _kb_config(exp),
        "tags": tag_map(),
    }
    if exp.uses_s3_vectors:
        bucket_arn, index_arn = ensure_vector_index(exp)
        kwargs["storageConfiguration"] = {
            "type": "S3_VECTORS",
            "s3VectorsConfiguration": {
                "vectorBucketArn": bucket_arn,
                "indexArn": index_arn,
            },
        }

    # CreateKnowledgeBase validates the role's S3 access, which can still fail
    # transiently while a just-written IAM policy propagates.
    last: Exception | None = None
    for attempt in range(5):
        try:
            kb_id = agent.create_knowledge_base(**kwargs)["knowledgeBase"]["knowledgeBaseId"]
            _wait_kb_active(exp, kb_id)
            return kb_id
        except Exception as exc:
            if not is_error(exc, "ValidationException", "AccessDeniedException"):
                raise
            last = exc
            time.sleep(15 * (attempt + 1))
    raise SystemExit(f"CreateKnowledgeBase kept failing for {exp.key}: {last}")


def _wait_kb_active(exp: Experiment, kb_id: str, timeout: int = 600) -> str:
    agent = client("bedrock-agent", exp.region)
    deadline = time.monotonic() + timeout
    status = "CREATING"
    while time.monotonic() < deadline:
        kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        status = kb["status"]
        if status == "ACTIVE":
            return status
        if status in ("FAILED", "DELETING", "DELETE_UNSUCCESSFUL"):
            reasons = "; ".join(kb.get("failureReasons", []) or ["no reason given"])
            raise SystemExit(f"Knowledge base {kb_id} entered {status}: {reasons}")
        time.sleep(6)
    raise SystemExit(f"Knowledge base {kb_id} still {status} after {timeout}s")


def _data_source_config(exp: Experiment, corpus_bucket: str) -> dict:
    bucket_arn = f"arn:aws:s3:::{corpus_bucket}"
    if exp.kb_type == "MANAGED":
        # A managed KB takes its S3 location through the managed connector,
        # which also controls its own multimodal extraction.
        return {
            "type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
            "managedKnowledgeBaseConnectorConfiguration": {
                "mediaExtractionConfiguration": {
                    "imageExtractionConfiguration": {
                        "imageExtractionStatus": exp.image_extraction
                    },
                },
                # connectorParameters is a document type, so boto3 validates
                # nothing here and a wrong shape is only reported
                # asynchronously as a FAILED data source. The managed connector
                # wants a flat connectionConfiguration keyed by bucket *name*
                # and owner account -- not the bucketArn that a plain S3 data
                # source takes. "version" is required; only "1" is accepted.
                "connectorParameters": {
                    "type": "S3",
                    "version": exp.connector_version,
                    "connectionConfiguration": {
                        "bucketName": corpus_bucket,
                        "bucketOwnerAccountId": account_id(),
                        "inclusionPrefixes": [CORPUS_PREFIX],
                    },
                },
            },
        }
    return {
        "type": "S3",
        "s3Configuration": {
            "bucketArn": bucket_arn,
            "inclusionPrefixes": [CORPUS_PREFIX],
        },
    }


def _ingestion_config(exp: Experiment) -> dict | None:
    """Chunking + parsing. The managed KB owns both, so we send nothing."""
    if exp.kb_type == "MANAGED":
        return None
    return {
        "chunkingConfiguration": {
            "chunkingStrategy": config.CHUNKING_STRATEGY,
            "fixedSizeChunkingConfiguration": {
                "maxTokens": config.CHUNK_MAX_TOKENS,
                "overlapPercentage": config.CHUNK_OVERLAP_PCT,
            },
        },
        "parsingConfiguration": {
            "parsingStrategy": exp.parsing_strategy,
            "bedrockFoundationModelConfiguration": {
                "modelArn": model_arn(exp.parser_model, exp.region),
                "parsingModality": exp.parsing_modality,
            },
        },
    }


def ensure_data_source(exp: Experiment, kb_id: str) -> str:
    """Create the data source.

    Data sources are deliberately untagged: `CreateDataSource` has no tags
    parameter and a data source has no ARN to tag afterwards. It carries no cost
    of its own, so nothing is lost for attribution -- the ingestion cost it
    drives bills as Bedrock model usage regardless.
    """
    agent = client("bedrock-agent", exp.region)
    ds_name = f"{exp.kb_name}-s3"

    # Ignore data sources that are on their way out or already broken -- reusing
    # a DELETING/FAILED id would hand back a source that can never ingest.
    for page in agent.get_paginator("list_data_sources").paginate(knowledgeBaseId=kb_id):
        for summary in page.get("dataSourceSummaries", []):
            if summary["name"] == ds_name and summary["status"] not in (
                "DELETING",
                "FAILED",
                "DELETE_UNSUCCESSFUL",
            ):
                return summary["dataSourceId"]

    corpus_bucket = ensure_bucket(exp.region)
    kwargs = {
        "knowledgeBaseId": kb_id,
        "name": ds_name,
        "description": f"Split STAR-CCM+ guide PDFs under s3://{corpus_bucket}/{CORPUS_PREFIX}",
        "dataSourceConfiguration": _data_source_config(exp, corpus_bucket),
        # Keep vectors when the data source is deleted -- avoids surprise
        # re-embedding costs on teardown/rebuild cycles.
        "dataDeletionPolicy": "RETAIN",
    }
    ingestion = _ingestion_config(exp)
    if ingestion:
        kwargs["vectorIngestionConfiguration"] = ingestion

    ds_id = agent.create_data_source(**kwargs)["dataSource"]["dataSourceId"]
    _wait_data_source_available(exp, kb_id, ds_id)
    return ds_id


def _wait_data_source_available(exp: Experiment, kb_id: str, ds_id: str, timeout: int = 180) -> str:
    """Poll a new data source to a terminal state.

    CreateDataSource returns 200 even when the configuration is invalid -- the
    managed connector validates asynchronously and only then flips to FAILED. So
    a silent success here would otherwise surface much later as an ingestion
    job that does nothing.
    """
    agent = client("bedrock-agent", exp.region)
    deadline = time.monotonic() + timeout
    status = "CREATING"
    while time.monotonic() < deadline:
        ds = agent.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)["dataSource"]
        status = ds["status"]
        if status == "AVAILABLE":
            return status
        if status in ("FAILED", "DELETING", "DELETE_UNSUCCESSFUL"):
            reasons = "; ".join(ds.get("failureReasons", []) or ["no reason given"])
            raise SystemExit(f"Data source {ds_id} for {exp.key} entered {status}: {reasons}")
        time.sleep(5)
    raise SystemExit(f"Data source {ds_id} still {status} after {timeout}s")


def provision(exp: Experiment) -> dict:
    """Full idempotent setup for one experiment. Returns the saved state."""
    ensure_bucket(exp.region)
    if exp.kb_type != "MANAGED":
        # FM-parser output target; must exist before the KB references it.
        ensure_bucket(exp.region, exp.supplemental_bucket(account_id()))
    if exp.uses_s3_vectors:
        # Called here rather than only from ensure_knowledge_base, which returns
        # early for an existing KB -- that path would never reach the vector
        # store, so its tags would never be applied on a re-run.
        ensure_vector_index(exp)
    role_arn = ensure_role(exp)
    kb_id = ensure_knowledge_base(exp, role_arn)
    ds_id = ensure_data_source(exp, kb_id)
    return save_state(
        exp.key,
        region=exp.region,
        role_arn=role_arn,
        knowledge_base_id=kb_id,
        data_source_id=ds_id,
        corpus_bucket=bucket_name(exp.region),
    )


def resolve(exp: Experiment) -> tuple[str, str]:
    """(knowledge_base_id, data_source_id) for an already-provisioned KB."""
    state = get_state(exp.key)
    kb_id, ds_id = state.get("knowledge_base_id"), state.get("data_source_id")
    if not kb_id or not ds_id:
        raise SystemExit(f"Experiment {exp.key!r} is not provisioned. Run `provision {exp.key}`.")
    return kb_id, ds_id
