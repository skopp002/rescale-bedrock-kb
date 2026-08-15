"""Configuration, loaded strictly from `config/config.yaml`.

There are deliberately **no defaults** in this module. Every value is read from
the YAML file, and a missing, null, or empty key raises `ConfigError` at import
time. The point is that an experiment can never run under a setting nobody
stated: a typo in a model id or a forgotten region fails loudly at build time
instead of producing a knowledge base that is quietly wrong.

The single exception is `aws.account_id`, which may be null because it is
discoverable via STS and must match the calling identity anyway.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("KB_CONFIG", REPO_ROOT / "config" / "config.yaml"))


class ConfigError(RuntimeError):
    """Raised when configuration is missing, empty, or malformed."""


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"No configuration file at {path}. Copy config/config.yaml into place "
            f"or point KB_CONFIG at one."
        )
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return raw


_RAW = _load(CONFIG_PATH)


def _require(*keys: str, allow_null: bool = False) -> Any:
    """Fetch a nested key, raising unless it is present and non-empty."""
    node: Any = _RAW
    trail: list[str] = []
    for key in keys:
        trail.append(key)
        if not isinstance(node, dict) or key not in node:
            raise ConfigError(f"{CONFIG_PATH}: missing required key '{'.'.join(trail)}'")
        node = node[key]

    if node is None:
        if allow_null:
            return None
        raise ConfigError(f"{CONFIG_PATH}: key '{'.'.join(keys)}' must not be null")
    # Reject empty strings and empty collections -- an empty list of subset
    # chapters or an empty model id is a configuration mistake, not a choice.
    if isinstance(node, (str, list, dict)) and len(node) == 0:
        raise ConfigError(f"{CONFIG_PATH}: key '{'.'.join(keys)}' must not be empty")
    return node


def _path(*keys: str) -> Path:
    value = _require(*keys)
    if not isinstance(value, str):
        raise ConfigError(f"{CONFIG_PATH}: key '{'.'.join(keys)}' must be a path string")
    return REPO_ROOT / value


# --- aws ---------------------------------------------------------------------
# The region for every experiment. Single-valued on purpose: an ingestion job
# cannot read a cross-region corpus bucket, so two regions means two copies of
# the corpus and two FM-parsing bills. `Experiment.region` reads this rather than
# a per-experiment key, which makes the split unrepresentable instead of merely
# discouraged -- see the rejection of `experiments.<key>.region` below.
PRIMARY_REGION: str = _require("aws", "primary_region")
ACCOUNT_ID: str | None = _require("aws", "account_id", allow_null=True)
PROJECT: str = _require("aws", "project_prefix")

# --- tags --------------------------------------------------------------------
# Cost-attribution tags for every resource that supports them. Kept as a plain
# dict because each AWS API wants a different shape; see aws.py for converters.
TAGS: dict[str, str] = dict(_require("tags"))

for _k, _v in TAGS.items():
    if not isinstance(_v, str) or not _v:
        raise ConfigError(
            f"{CONFIG_PATH}: tags.{_k} must be a non-empty string (got {_v!r}). "
            f"An empty tag value is silently useless for cost attribution."
        )

# --- paths -------------------------------------------------------------------
DATA_DIR: Path = _path("paths", "data_dir")
SOURCE_PDF: Path = _path("paths", "source_pdf")
SPLIT_DIR: Path = _path("paths", "split_dir")
EVAL_QUESTIONS: Path = _path("paths", "eval_questions")
RESULTS_DIR: Path = _path("paths", "results_dir")
STATE_FILE: Path = _path("paths", "state_file")
CORPUS_PREFIX: str = _require("paths", "corpus_prefix")
MANIFEST_NAME: str = _require("paths", "manifest_name")

# --- corpus ------------------------------------------------------------------
SUBSET_CHAPTERS: tuple[str, ...] = tuple(_require("corpus", "subset_chapters"))
MAX_PAGES_PER_PART: int = _require("corpus", "max_pages_per_part")

# --- chunking ----------------------------------------------------------------
CHUNKING_STRATEGY: str = _require("chunking", "strategy")
CHUNK_MAX_TOKENS: int = _require("chunking", "max_tokens")
CHUNK_OVERLAP_PCT: int = _require("chunking", "overlap_percentage")

# --- models ------------------------------------------------------------------
# Prefixes that mark a model id as a cross-region inference profile rather than
# an on-demand foundation model. They resolve to a different ARN resource type
# (`inference-profile/` not `foundation-model/`), so a missing prefix here builds
# a malformed ARN. `jp.` (Tokyo) and `apac.` are easy to forget.
INFERENCE_PROFILE_PREFIXES: tuple[str, ...] = ("us.", "eu.", "jp.", "apac.", "global.")

FM_PARSER_MODEL: str = _require("models", "fm_parser")
GENERATION_MODEL: str = _require("models", "generation")
JUDGE_MODEL: str = _require("models", "judge")

if JUDGE_MODEL == GENERATION_MODEL:
    # A model cannot grade its own output: the mistakes it makes are precisely
    # the ones it cannot recognise, so self-grading inflates every score.
    raise ConfigError(
        f"{CONFIG_PATH}: models.judge must differ from models.generation "
        f"(both are {JUDGE_MODEL!r}). A model grading its own answers reports "
        f"its own blind spots as correct."
    )


@dataclass(frozen=True)
class InferenceParams:
    """Converse inference config for one role.

    `temperature` is None when the parameter must be omitted -- Claude 4.6 and
    later reject it as deprecated with a ValidationException.
    """

    max_tokens: int
    temperature: float | None

    def as_config(self) -> dict:
        cfg: dict = {"maxTokens": self.max_tokens}
        if self.temperature is not None:
            cfg["temperature"] = self.temperature
        return cfg


def _inference(role: str) -> InferenceParams:
    return InferenceParams(
        max_tokens=_require("inference", role, "max_tokens"),
        temperature=_require("inference", role, "temperature", allow_null=True),
    )


GENERATION_INFERENCE: InferenceParams = _inference("generation")
JUDGE_INFERENCE: InferenceParams = _inference("judge")

# --- retrieval ---------------------------------------------------------------
TOP_K: int = _require("retrieval", "top_k")
NON_FILTERABLE_METADATA_KEYS: list[str] = list(_require("retrieval", "non_filterable_metadata_keys"))
DISTANCE_METRIC: str = _require("retrieval", "distance_metric")

DEFAULT_EXPERIMENTS: tuple[str, ...] = tuple(_require("default_experiments"))


@dataclass(frozen=True)
class Experiment:
    """One knowledge base under test.

    `kb_type` selects the Bedrock KB flavour:
      - "VECTOR"  -> self-managed vector store (S3 Vectors here), our own
                     chunking and BEDROCK_FOUNDATION_MODEL parsing.
      - "MANAGED" -> Bedrock Managed Knowledge Base: AWS owns the vector store,
                     chunking, embeddings, and parsing (SMART_PARSING).

    There is no `region` field: every experiment runs in `aws.primary_region`.
    See the `region` property.
    """

    key: str
    label: str
    kb_type: str
    parsing_strategy: str
    uses_s3_vectors: bool
    notes: str
    embedding_model: str | None
    embedding_dims: int | None
    embedding_data_type: str | None
    parsing_modality: str | None
    parser_model: str | None
    connector_version: str | None
    image_extraction: str | None

    @property
    def region(self) -> str:
        """Always `aws.primary_region`.

        Kept as a property rather than dropped so every call site still reads
        `exp.region` -- the region is genuinely a property of an experiment, it
        just isn't independently chosen. Every experiment sharing one region is
        what lets them share one corpus bucket, and therefore one FM-parsing
        bill, since an ingestion job cannot read across regions.
        """
        return PRIMARY_REGION

    @property
    def kb_name(self) -> str:
        return f"{PROJECT}-{self.key}"

    @property
    def vector_bucket(self) -> str:
        return f"{PROJECT}-vectors-{self.key}"

    @property
    def vector_index(self) -> str:
        return f"{PROJECT}-{self.key}-index"

    @property
    def role_name(self) -> str:
        return f"{PROJECT}-{self.key}-kb-role"

    def supplemental_bucket(self, account_id: str) -> str:
        """Where the FM parser writes extracted figures/tables.

        Must be a bucket root -- Bedrock rejects a supplemental data URI
        containing any sub-folder -- so it cannot be a prefix inside the corpus
        bucket. Takes the account id explicitly because resolving it needs STS.
        """
        return f"{PROJECT}-multimodal-{account_id}-{self.key}"


_VALID_KB_TYPES = ("VECTOR", "MANAGED")


def _build_experiment(key: str, spec: dict[str, Any]) -> Experiment:
    """Validate one experiment block. Requirements depend on the KB type."""
    if not isinstance(spec, dict):
        raise ConfigError(f"{CONFIG_PATH}: experiments.{key} must be a mapping")

    def field(name: str, *, required: bool) -> Any:
        if name not in spec:
            raise ConfigError(f"{CONFIG_PATH}: missing key 'experiments.{key}.{name}'")
        value = spec[name]
        if required and (value is None or (isinstance(value, str) and not value)):
            raise ConfigError(f"{CONFIG_PATH}: 'experiments.{key}.{name}' must be set")
        return value

    # Rejected rather than ignored. A stale `region:` left in an experiment block
    # would otherwise read as though it were in force, which is exactly the kind
    # of silently-wrong configuration this module exists to prevent.
    if "region" in spec:
        raise ConfigError(
            f"{CONFIG_PATH}: 'experiments.{key}.region' is not a valid key. Every "
            f"experiment runs in aws.primary_region ({PRIMARY_REGION}); a "
            f"per-experiment region would need its own copy of the corpus, "
            f"because an ingestion job cannot read cross-region."
        )

    kb_type = field("kb_type", required=True)
    if kb_type not in _VALID_KB_TYPES:
        raise ConfigError(
            f"{CONFIG_PATH}: experiments.{key}.kb_type must be one of "
            f"{', '.join(_VALID_KB_TYPES)} (got {kb_type!r})"
        )

    # A self-managed KB must name its embedding model, dimensions, and parser
    # modality; a managed KB must not, because AWS supplies them.
    is_vector = kb_type == "VECTOR"
    exp = Experiment(
        key=key,
        label=field("label", required=True),
        kb_type=kb_type,
        parsing_strategy=field("parsing_strategy", required=True),
        uses_s3_vectors=field("uses_s3_vectors", required=True),
        notes=field("notes", required=True),
        embedding_model=field("embedding_model", required=is_vector),
        embedding_dims=field("embedding_dims", required=is_vector),
        embedding_data_type=field("embedding_data_type", required=is_vector),
        parsing_modality=field("parsing_modality", required=is_vector),
        # The FM parser is shared across experiments, so it lives under models.
        parser_model=FM_PARSER_MODEL if is_vector else None,
        connector_version=field("connector_version", required=True) if not is_vector else None,
        image_extraction=field("image_extraction", required=True) if not is_vector else None,
    )

    if not isinstance(exp.uses_s3_vectors, bool):
        raise ConfigError(f"{CONFIG_PATH}: experiments.{key}.uses_s3_vectors must be a boolean")
    if is_vector and not isinstance(exp.embedding_dims, int):
        raise ConfigError(f"{CONFIG_PATH}: experiments.{key}.embedding_dims must be an integer")
    return exp


EXPERIMENTS: dict[str, Experiment] = {
    key: _build_experiment(key, spec) for key, spec in _require("experiments").items()
}

# Fail at import time if default_experiments names something undefined.
for _key in DEFAULT_EXPERIMENTS:
    if _key not in EXPERIMENTS:
        raise ConfigError(
            f"{CONFIG_PATH}: default_experiments lists unknown experiment {_key!r}. "
            f"Defined: {', '.join(EXPERIMENTS)}"
        )


def experiment(key: str) -> Experiment:
    try:
        return EXPERIMENTS[key]
    except KeyError:
        raise SystemExit(
            f"Unknown experiment {key!r}. Choose from: {', '.join(EXPERIMENTS)}"
        ) from None
