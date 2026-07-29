"""Upload the split corpus to S3 as a Bedrock KB data source.

Each PDF gets a sidecar `<key>.metadata.json`, which is how Bedrock KB attaches
filterable metadata to every chunk derived from that file. Carrying the chapter
and source page range through means a retrieval result can be traced to a page
in the original 14,125-page guide.
"""

from __future__ import annotations

import json
from pathlib import Path

from boto3.s3.transfer import TransferConfig

from . import config
from .aws import account_id, client, is_error
from .split import Part

CORPUS_PREFIX = config.CORPUS_PREFIX

# The parts are a few MB each; multipart above 16 MB is plenty.
_TRANSFER = TransferConfig(multipart_threshold=16 * 1024 * 1024, max_concurrency=8)


def bucket_name(region: str) -> str:
    return f"{config.PROJECT}-corpus-{account_id()}-{region}"


def _tag_bucket(s3, name: str) -> None:
    """Apply the project tags, preserving any tags already on the bucket.

    PutBucketTagging *replaces* the entire tag set rather than merging, so the
    existing set has to be read first -- otherwise tagging would silently strip
    tags this project didn't create.
    """
    existing: dict[str, str] = {}
    try:
        current = s3.get_bucket_tagging(Bucket=name)["TagSet"]
        existing = {t["Key"]: t["Value"] for t in current}
    except Exception as exc:
        # No tag set at all is reported as an error, not an empty list.
        if not is_error(exc, "NoSuchTagSet", "NoSuchTagSetError", "404", "NoSuchBucket"):
            raise

    merged = existing | dict(config.TAGS)
    if merged == existing:
        return
    s3.put_bucket_tagging(
        Bucket=name,
        Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in merged.items()]},
    )


def ensure_bucket(region: str, name: str | None = None) -> str:
    """Create a bucket if absent. Defaults to the corpus bucket for `region`.

    Tags are applied whether or not the bucket already existed, so adding a tag
    to the config propagates to buckets built before it was added.
    """
    name = name or bucket_name(region)
    s3 = client("s3", region)
    try:
        s3.head_bucket(Bucket=name)
        _tag_bucket(s3, name)
        return name
    except Exception as exc:
        if not is_error(exc, "404", "NoSuchBucket", "403", "AccessDenied"):
            raise

    kwargs = {"Bucket": name}
    # us-east-1 is the one region that rejects an explicit LocationConstraint.
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        s3.create_bucket(**kwargs)
    except Exception as exc:
        if not is_error(exc, "BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    _tag_bucket(s3, name)
    return name


def metadata_for(part: Part) -> dict:
    """Bedrock KB sidecar metadata. Keys become filterable chunk attributes.

    The corpus is physically many objects but logically one guide. These
    attributes are what re-unify it at query time: `source_pdf` scopes a query
    to the whole guide, `chapter` to one chapter, and the page bounds to an
    arbitrary page window -- so splitting costs nothing in addressability.
    """
    return {
        "metadataAttributes": {
            "chapter": {"value": {"type": "STRING", "stringValue": part.chapter}, "includeForEmbedding": True},
            "source_pdf": {
                "value": {"type": "STRING", "stringValue": config.SOURCE_PDF.name},
                "includeForEmbedding": False,
            },
            # Which slice of a multi-part chapter this is, for provenance.
            "part": {
                "value": {"type": "STRING", "stringValue": f"{part.part_index}/{part.part_count}"},
                "includeForEmbedding": False,
            },
            "start_page": {
                "value": {"type": "NUMBER", "numberValue": part.start_page + 1},
                "includeForEmbedding": False,
            },
            "end_page": {
                "value": {"type": "NUMBER", "numberValue": part.end_page + 1},
                "includeForEmbedding": False,
            },
            "pages": {
                "value": {"type": "NUMBER", "numberValue": part.pages},
                "includeForEmbedding": False,
            },
        }
    }


def _needs_upload(s3, bucket: str, key: str, local: Path) -> bool:
    """Skip re-uploading identical objects -- size match is enough here, since
    the parts are deterministic output of the splitter."""
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if is_error(exc, "404", "NoSuchKey"):
            return True
        raise
    return head["ContentLength"] != local.stat().st_size


def upload_parts(
    parts: list[Part],
    region: str,
    split_dir: Path | None = None,
    force: bool = False,
):
    """Upload each part plus its metadata sidecar. Yields (part, uploaded)."""
    split_dir = split_dir or config.SPLIT_DIR
    bucket = ensure_bucket(region)
    s3 = client("s3", region)

    for part in parts:
        local = split_dir / part.filename
        if not local.exists():
            raise SystemExit(f"Missing split file {local}. Run `split` first.")
        key = f"{CORPUS_PREFIX}{part.filename}"

        if force or _needs_upload(s3, bucket, key, local):
            s3.upload_file(
                str(local),
                bucket,
                key,
                ExtraArgs={"ContentType": "application/pdf"},
                Config=_TRANSFER,
            )
            uploaded = True
        else:
            uploaded = False

        # The sidecar is small; always write it so metadata fixes propagate.
        s3.put_object(
            Bucket=bucket,
            Key=f"{key}.metadata.json",
            Body=json.dumps(metadata_for(part)).encode(),
            ContentType="application/json",
        )
        yield part, uploaded


def list_corpus(region: str) -> list[dict]:
    s3 = client("s3", region)
    bucket = bucket_name(region)
    objects: list[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=CORPUS_PREFIX):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".metadata.json"):
                    continue
                objects.append(obj)
    except Exception as exc:
        if is_error(exc, "NoSuchBucket", "404"):
            return []
        raise
    return objects
