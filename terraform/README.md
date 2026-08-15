# The same three knowledge bases, in Terraform

A declarative build of everything `kb upload` and `kb provision` do — all three
experiments from `config/config.yaml`, in one `terraform apply`.

This does not replace the Python stack. It stands beside it: `name_suffix = "tf"`
means every resource here is named `starccm-kb-tf-*` where the Python one is
`starccm-kb-*`, so both exist in the account at once and neither can adopt or
clobber the other's resources.

## Assessment: what Terraform can and cannot do here

All three knowledge-base shapes are expressible. The provider floor is **AWS
6.56.0**, which is recent — that release (July 2026) added the Managed Knowledge
Base type. Verified against the provider source, not assumed:

| need | resource / argument | since |
|---|---|---|
| S3 Vectors store | `aws_s3vectors_vector_bucket`, `aws_s3vectors_index` | 6.24.0 |
| non-filterable metadata keys | `aws_s3vectors_index.metadata_configuration` | 6.25.0 |
| KB → S3 Vectors | `storage_configuration.s3_vectors_configuration` | 6.27.0 |
| **Managed KB** | `knowledge_base_configuration.managed_knowledge_base_configuration` | **6.56.0** |
| **Managed connector data source** | `data_source_configuration.managed_knowledge_base_connector_configuration` | **6.56.0** |
| FM parser output target | `supplemental_data_storage_configuration` | earlier |

`connector_parameters` is a `jsonencode(...)` string — the same document-type
passthrough as boto3 — so the hard-won managed-connector shape ports verbatim.

### One region, enforced by absence

Every experiment runs in `aws.primary_region`, so **no resource in this module
carries a `region` argument**. The provider block is the only place a region is
stated, which means the per-resource `region` override the AWS provider offers
(6.0.0) is simply never used and a cross-region stack cannot be expressed. A
leftover `experiments.<key>.region` in `config.yaml` fails the plan on a
precondition rather than being silently ignored, matching `config.py`.

That is also why there is one corpus bucket rather than one per region: an
ingestion job cannot read cross-region, so a split region would fan the corpus
out into a full copy per region — and a second FM-parsing bill over the same
PDFs.

### The one real gap: ingestion

**There is no `StartIngestionJob` resource in the AWS provider.** Terraform can
build every resource in this project but cannot make a knowledge base queryable.
That is the same class of gap as "nothing in CloudFormation can execute SQL" in
the Aurora assessment, and it is why `kb ingest` is still the next step after
`apply`. Wrapping it in a `local-exec` was rejected: it would discard the
polling, per-document counts, and failure-reason reporting that `ingest.py`
already does, and make re-runs trigger-driven rather than idempotent.

`split` also stays in Python — cutting a 14,125-page PDF on its own outline is
not Terraform's job. This module consumes the manifest that `kb split` writes.

### Two residual differences from the Python stack

- **IAM propagation is a wait, not a retry.** `CreateKnowledgeBase` validates the
  role's S3 access, and IAM is eventually consistent. `provision.py` sleeps *and*
  retries five times; Terraform has no per-resource retry hook, so all this
  module has is `time_sleep` (`iam_propagation_delay`, default 25s). If an apply
  still loses the race it fails loudly and a re-apply succeeds.
- **Asynchronous data-source failure may be quieter.** `CreateDataSource` returns
  success and then flips to `FAILED` when `connector_parameters` is malformed;
  `_wait_data_source_available` exists in the Python precisely to surface that
  immediately. Whether the provider waits for a terminal state is unverified, so
  check `kb status` after the first apply rather than trusting a green apply.

## config.yaml is still the single source of truth

The module reads `../config/config.yaml` with `yamldecode` and derives everything
from it. Nothing is duplicated, and **nothing is defaulted** — the same contract
`config.py` enforces. Values are read by direct attribute access, so a missing
key fails the plan:

```
Error: Unsupported attribute
  59:       embedding_dims      = v.kb_type == "VECTOR" ? v.embedding_dims : null
This object does not have an attribute named "embedding_dims".
```

The invariants that attribute access can't express are preconditions on
`terraform_data.validate_config`, and a failed precondition fails the plan, so
nothing is created under a configuration the Python loader would have rejected:
`models.judge != models.generation`, per-`kb_type` required fields, no
per-experiment `region`, non-empty tag values, a pinned `aws.account_id` matching
the caller, and `float32` for any S3-Vectors-backed experiment.

All of the above were verified by planning against deliberately broken configs.

## Usage

```bash
uv run kb split                      # writes data/split/ + manifest.json (required)
aws sso login                        # AdministratorAccess

cd terraform
terraform init
terraform plan                       # titan + managed, per default_experiments
terraform apply

# Terraform cannot ingest. Drive the rest of the pipeline with the generated
# config, which points the CLI at this stack instead of the Python-built one.
cd ..
export KB_CONFIG=terraform/config.tf.yaml
uv run kb ingest                     # parse + chunk + embed
uv run kb status
uv run kb eval
uv run kb compare
```

`terraform plan -var 'experiments=["all"]'` adds `nova` (41 resources instead of
31). It shares the one corpus bucket with the others, so no second copy of the
corpus is uploaded — but it does FM-parse that corpus into its own vector index,
which is the cost below.

**Cost before you apply.** `titan` and `nova` each FM-parse the whole corpus on
first ingest, and that is the largest single cost in this project. Building this
stack alongside the Python one means paying it again.

### The handoff files

`apply` writes two gitignored files:

| file | why |
|---|---|
| `kb-state.json` | The exact shape `aws.save_state()` writes, so `provision.resolve()` finds these KBs with no change to the Python code. |
| `config.tf.yaml` | `config.yaml` with **two** keys overridden — `aws.project_prefix` and `paths.state_file` — so the CLI addresses this stack. Derived, not copied: adding a key to `config.yaml` propagates on the next apply. |

Deliberately *not* `.kb-state.json`: that file belongs to the Python-built stack,
and overwriting it would strand those knowledge bases.

## Layout

```
versions.tf          provider floors, with the reason for each
providers.tf         the one region + default_tags, both from config.yaml
variables.tf         only where this stack sits; what to build is in config.yaml
config.tf            yamldecode, normalisation, and the validation preconditions
s3.tf                the corpus bucket + FM-parser supplemental buckets
corpus.tf            split PDFs + metadata sidecars, from the manifest
iam.tf               KB role and its per-kb_type policy, + the propagation wait
vectors.tf           S3 Vectors bucket and index (VECTOR experiments only)
knowledge_base.tf    the VECTOR / MANAGED split
data_source.tf       S3 vs MANAGED_KNOWLEDGE_BASE_CONNECTOR
handoff.tf           kb-state.json + config.tf.yaml for the Python CLI
outputs.tf           resource IDs and the remaining pipeline steps
```

## Teardown

```bash
terraform destroy -var force_destroy=true
```

`force_destroy` is off by default because the buckets and vector indexes are not
empty, and destroying them means paying to re-embed and re-parse. Data sources
carry `data_deletion_policy = "RETAIN"` for the same reason.

## One deliberate divergence from provision.py

For the managed KB, `provision.py` sends no `vectorIngestionConfiguration` at all
("the managed KB owns both, so we send nothing"). This module sends
`parsing_strategy = "SMART_PARSING"`. Both are accepted — `SMART_PARSING` is in
the SDK's `ParsingStrategy` enum and the provider's discriminator only constrains
`BEDROCK_FOUNDATION_MODEL` — and sending it means
`experiments.managed.parsing_strategy` in `config.yaml` is actually used rather
than silently ignored, which is that file's whole premise. If a future API change
rejects it, drop the block for `MANAGED`: `SMART_PARSING` is the managed default
either way.
