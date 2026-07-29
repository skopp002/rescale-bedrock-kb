# Two Bedrock knowledge bases, one accuracy comparison

Builds three Amazon Bedrock knowledge bases over the same corpus — the Simcenter
STAR-CCM+ user guide — and scores them against one hand-authored eval set, so the
only variable between them is the knowledge base itself.

| key | knowledge base | vector store | embeddings | parsing | region |
|---|---|---|---|---|---|
| `titan` | Bedrock KB (`VECTOR`) | S3 Vectors (ours) | Titan Text v2, 1024-d | FM parser, `MULTIMODAL` | us-west-2 |
| `nova` | Bedrock KB (`VECTOR`) | S3 Vectors (ours) | Nova 2 multimodal, 1024-d | FM parser, `MULTIMODAL` | us-east-1 |
| `managed` | Bedrock **Managed** KB | AWS-owned | AWS-selected | `SMART_PARSING` | us-west-2 |

`titan` and `nova` are the same knowledge base differing only in embedding model,
which isolates what a multimodal embedder buys you. `managed` is the second
knowledge base type — AWS owns the vector store, chunking, embeddings, and parser.

## Results

16 questions, `k=5`. Full per-question detail in `results/*.json`.

| metric | titan | nova | managed |
|---|---|---|---|
| recall@5 | 1.0 | 1.0 | 1.0 |
| precision@5 | 0.825 | **0.900** | 0.850 |
| MRR | 0.9688 | **1.0** | 0.9688 |
| hit@1 | 0.9375 | **1.0** | 0.9375 |
| answer_correctness | 0.7917 | 0.7917 | 0.7917 |
| keyword_coverage | 0.9688 | 0.9688 | **1.0** |
| citation_precision | 1.0 | 1.0 | 1.0 |
| retrieve latency p50 | **619 ms** | 851 ms | 808 ms |
| RAG latency p50 | 3254 ms | 3303 ms | **2798 ms** |

Read these with the caveats attached:

- **`recall@5 = 1.0` everywhere means the metric is saturated, not that the
  systems are equal.** With five chapters indexed and questions written against
  known page ranges, finding *something* relevant in the top 5 is easy. Ranking
  quality (MRR, hit@1) still separates them, and that's where Nova wins: it put a
  correct chunk first on all 16 questions.
- **`answer_correctness` being identical at 0.7917 is a coincidence, not a bug.**
  Per-question judge scores differ (`titan` 3,2,3,3,2,… vs `nova` 3,3,3,3,2,…);
  all three happen to total 38/48. Verify with
  `jq '[.answers[].judge_score]' results/*.json`.
- **No figure/table questions are in the eval set yet**, so `recall_by_kind`
  reports only `text`. That is exactly the axis on which a multimodal embedder
  should separate from a text-only one, so the Nova-vs-Titan comparison is
  currently incomplete — the gap is in the eval set, not the pipeline.
- The corpus is a **539-page subset** (5 chapters) of the 14,125-page guide.

## Why the PDF is split

`data/` is one 240 MB, 14,125-page PDF. The Bedrock quota
`(Knowledge Bases) Ingestion job file size with text content` is **50 MB**, so
ingesting it as a single object is impossible — the split is mandatory, not an
optimization. It also cuts FM-parsing cost, since parsing bills on input/output
tokens (only Bedrock Data Automation bills per page).

`kb split` cuts on the PDF's **own outline**, so parts are topically coherent and
chunks don't straddle unrelated chapters. Chapters over
`corpus.max_pages_per_part` are divided into equal parts. The source page range
is encoded in each filename:

```
design-manager__p07763-07997.pdf
```

### Staying one logical document

Each object gets a `<key>.metadata.json` sidecar so the split is invisible at
query time:

```json
{"metadataAttributes": {
  "chapter":     {"value": {"type": "STRING", "stringValue": "Design Manager"}, "includeForEmbedding": true},
  "source_pdf":  {"value": {"type": "STRING", "stringValue": "userguide_20.06.007_en.pdf"}, "includeForEmbedding": false},
  "start_page":  {"value": {"type": "NUMBER", "numberValue": 7763}, "includeForEmbedding": false},
  "end_page":    {"value": {"type": "NUMBER", "numberValue": 7997}, "includeForEmbedding": false}
}}
```

`chapter` is embedded because a chapter title is real semantic context; page
numbers are not, because they would pollute the vectors. `source_pdf` is what
re-unifies the parts into one logical document.

Those attributes are queryable filters, which is how you scope a question back to
a region of the original guide:

```bash
kb ask "How do I define a cost function?" -e titan --chapter "Design Manager"
kb ask "What are the hardware requirements?" -e titan --page-from 100 --page-to 189
```

Page bounds match on **overlap** (`end_page >= from AND start_page <= to`), so a
part that straddles the window still matches. Citations render as pages in the
original guide: `Design Manager (guide pp. 7763-7997)`.

## Configuration

`config/config.yaml` is the single source of truth. **There are no defaults
anywhere in the code** — a missing, null, or empty value raises `ConfigError` at
import time rather than falling back to something plausible, so an experiment
can't run under settings nobody stated. Two rules are enforced structurally:

- `models.judge` **must differ** from `models.generation`. A model grading its
  own answers reports its own blind spots as correct.
- `aws.account_id` is the only key permitted to be null, because STS can
  discover it.

Point `KB_CONFIG` at another file to use a different config.

## Cost attribution

Every resource this project creates that supports tagging carries
`project_name=rescale-kb-evals`, set under `tags:` in `config/config.yaml`.
`kb provision` applies tags to resources that **already exist**, so adding a key
propagates on the next run rather than only to newly built resources.

Two limits are worth knowing before trusting these in Cost Explorer:

- **Bedrock model invocation is not tag-attributable.** FM parsing, embedding,
  generation, and judging all bill as account-level model usage, not against the
  tagged knowledge base. FM-parsing the corpus is likely the largest single cost
  here, and tags will not capture it.
- **A tag reaches Cost Explorer only after activation.** `project_name` must be
  activated as a cost allocation tag in the payer account's Billing console — a
  manual, one-time step with no API, and it is not retroactive.

`AWS::Bedrock::DataSource` / `CreateDataSource` has **no tag support** and a data
source has no ARN to tag afterwards, so data sources are deliberately untagged.
They carry no cost of their own, so nothing is lost.

The same tags go out in three shapes, because AWS never settled on one — IAM
takes `[{Key,Value}]`, S3 takes `{TagSet:[...]}`, and Bedrock and S3 Vectors take
a flat map. `aws.py` has one converter each. S3's `PutBucketTagging` *replaces*
the whole tag set, so bucket tagging reads the current set and merges, rather
than stripping tags this project didn't create.

## Workflow

```bash
uv sync --extra dev
aws sso login                      # AdministratorAccess

uv run kb experiments              # what's configured
uv run kb split                    # PDF -> data/split/ + manifest
uv run kb upload all               # -> s3://<prefix>-corpus-<acct>-<region>/corpus/
uv run kb provision all            # IAM role, S3 Vectors, KB, data source
uv run kb ingest all               # parse + chunk + embed ("sync" in the console)
uv run kb status                   # resource IDs + last ingestion job
uv run kb eval all                 # -> results/<key>.json
uv run kb compare                  # side-by-side table
```

Every stage is idempotent; re-running reuses whatever exists. Resource IDs land
in `.kb-state.json` so later stages find them without pasting IDs around.

## How accuracy is measured

`evals/questions.json` holds 16 questions written against text sampled from the
split PDFs, each with `expected_pages`, a `reference_answer`, and
`expected_keywords`.

**Retrieval** — recall@k, precision@k, MRR, hit@1, scored by *page-range
overlap*, with no model involved. Chunk-id matching would be meaningless here
because the managed KB chunks differently from ours; page ranges are comparable
across all three.

**Answer** — an LLM judge (Sonnet 5) grades the generated answer 0-3 against the
reference answer, normalized so 1.0 is perfect. The judge compares two texts; it
never decides what is true about STAR-CCM+ on its own. `keyword_coverage` is
literal substring matching, so it corroborates the judge without a model.

Managed KBs **do not support `RetrieveAndGenerate`**, so answers are generated
client-side from retrieved chunks via `bedrock-runtime.converse`. Every
experiment therefore uses the same generation model and the same prompt, which
makes `answer_correctness` a measure of retrieval quality rather than of
differing server-side RAG implementations.

## Notes from building this

Things that cost real debugging time:

- **S3 Vectors caps *filterable* metadata at 2 KB per vector.** Bedrock writes
  both `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA`; omitting either from
  `nonFilterableMetadataKeys` fails every `PutVectors` call mid-ingestion. The
  index can't be amended afterward — it has to be recreated.
- **`supplementalDataStorageConfiguration` must be a bucket root.** Any
  sub-folder in the URI is rejected, so it can't be a prefix in the corpus bucket.
- **`connectorParameters` on a managed data source is a `document` type**, so
  boto3 validates nothing. `CreateDataSource` returns 200 and the source flips to
  `FAILED` asynchronously. It needs `type: "S3"`, `version: "1"` (only `"1"` is
  accepted), and a *flat* `connectionConfiguration` with `bucketName` +
  `bucketOwnerAccountId` — not the `bucketArn` a plain S3 source takes. The code
  polls to a terminal state so this surfaces immediately.
- **Managed KBs need `managedSearchConfiguration`**, not
  `vectorSearchConfiguration`. Same filter grammar, no `overrideSearchType`.
- **`CreateKnowledgeBase` validates the role's S3 access**, so a just-written IAM
  policy needs a propagation wait plus retry — including on re-runs where the
  role already exists.
- **Claude 4.6+ rejects `temperature`** with a `ValidationException`. Hence
  `inference.<role>.temperature: null` meaning "omit the key".
- **`amazon.nova-lite-v1:0` is the FM parser** because it's the cheapest current
  vision model in us-west-2 still supporting `ON_DEMAND` — newer ones are
  `INFERENCE_PROFILE`-only and need a profile ARN.

## Layout

```
config/config.yaml    every setting; no defaults in code
data/                 source PDF + data/split/ (gitignored)
evals/questions.json  16 ground-truth questions
results/              per-experiment eval reports
src/rescale_bedrock_kb/
  config.py           strict YAML loader
  split.py            outline-aware PDF splitting
  upload.py           S3 upload + metadata sidecars
  provision.py        IAM, S3 Vectors, KB, data source
  ingest.py           ingestion jobs
  query.py            retrieve / retrieve-and-generate
  evaluate.py         metrics + LLM judge
  cli.py              typer commands
tests/                35 tests, no AWS calls
```

## Next

- Add figure/table questions — the one axis where a multimodal embedder should
  separate from a text-only one, and currently untested.
- Harden the eval: `recall@5` is saturated at 1.0, so add distractor-heavy
  questions or drop to `k=1`/`k=3` to regain discrimination.
- Scale to the full 14,125 pages (`kb split --no-subset`). Note the
  `Maximum number of files for Foundation Models as a parser` quota of 1000.

### Aurora PostgreSQL + pgvector as the vector store

Requested as an alternative to S3 Vectors, to be built as a CloudFormation stack
rather than through APIs. Assessed against the live CFN registry; **viable, with
one structural gap.** Every construct needed exists:

| need | CFN resource | |
|---|---|---|
| Aurora PG cluster | `AWS::RDS::DBCluster` | incl. `ServerlessV2ScalingConfiguration`, `EnableHttpEndpoint`, `ManageMasterUserPassword` |
| credentials | `AWS::SecretsManager::Secret` / `MasterUserSecret` | |
| KB → Aurora | `AWS::Bedrock::KnowledgeBase` → `StorageConfiguration.RdsConfiguration` | `RDS` is in the `KnowledgeBaseStorageType` enum |
| VPC / subnets / SG | `AWS::EC2::*`, `AWS::RDS::DBSubnetGroup` | |

The existing three experiments are also fully expressible in CFN — `MANAGED` is
in the `KnowledgeBaseType` enum and `MANAGED_KNOWLEDGE_BASE_CONNECTOR` in
`DataSourceType`, so a single template could cover all four.

**The gap: nothing in CloudFormation can execute SQL.** `RdsConfiguration`
requires `ResourceArn`, `CredentialsSecretArn`, `DatabaseName`, `TableName`, and
`FieldMapping`, and Bedrock validates that the table *already exists* at
`CreateKnowledgeBase` time. So `CREATE EXTENSION vector`, the schema, the table,
and an HNSW index must all run before the KB resource is created. The registry
has no first-party SQL-executing resource (only third-party types like
`Generic::Database::Schema`), so this needs a **Lambda-backed custom resource**
using the RDS Data API, sequenced with `DependsOn`. "Entire solution in
CloudFormation" therefore means ~95% declarative plus one custom resource that is
real code to write, test, and handle on delete/rollback.

Two things to settle before writing the template:

- **Does the chosen `aurora-postgresql` version support the Data API?**
  `describe-db-engine-versions` returns no `SupportsHttpEndpoint` field at all
  (absent, not `false`), so this was inconclusive. If unavailable, the bootstrap
  Lambda must run *inside the VPC* with `psycopg`, which adds subnets, a security
  group, and a layer — a materially different networking section.
- **Cost profile changes character.** S3 Vectors is pay-per-use; Aurora
  Serverless v2 bills continuously from a ~0.5 ACU floor whether or not an eval
  is running. For periodic accuracy comparison that is a standing cost, which
  argues for treating the stack as ephemeral. Aurora *is* captured by the cost
  tags above, unlike Bedrock model usage.

Suggested sequencing: add Aurora as a **fourth experiment (`aurora`) rather than
a replacement**. This repo's premise is that the KB is the only variable, so
keeping S3 Vectors alongside makes vector-store choice a measurable axis instead
of an untested swap — and de-risks the migration.
