"""AWS client construction and small shared helpers.

Clients are cached per (service, region) because the Nova experiment runs in a
different region from everything else and we build clients on demand all over
the pipeline.
"""

from __future__ import annotations

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from . import config

_BOTO_CONFIG = Config(
    retries={"max_attempts": 8, "mode": "adaptive"},
    read_timeout=120,
    connect_timeout=15,
)


@cache
def client(service: str, region: str | None = None):
    return boto3.client(
        service,
        region_name=region or config.PRIMARY_REGION,
        config=_BOTO_CONFIG,
    )


@lru_cache(maxsize=1)
def account_id() -> str:
    if config.ACCOUNT_ID:
        return config.ACCOUNT_ID
    return client("sts").get_caller_identity()["Account"]


def is_error(exc: Exception, *codes: str) -> bool:
    """True when a ClientError carries one of `codes`."""
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code") in codes


# --- tags --------------------------------------------------------------------
# The same tags in three different shapes, because AWS never settled on one:
#   IAM        -> [{"Key": k, "Value": v}]
#   S3         -> {"TagSet": [{"Key": k, "Value": v}]}
#   Bedrock,
#   S3 Vectors -> {k: v}


def tag_map() -> dict[str, str]:
    """Tags as a flat map (bedrock-agent, s3vectors)."""
    return dict(config.TAGS)


def tag_list() -> list[dict[str, str]]:
    """Tags as a Key/Value list (IAM)."""
    return [{"Key": k, "Value": v} for k, v in config.TAGS.items()]


def tag_set() -> dict[str, list[dict[str, str]]]:
    """Tags as an S3 TagSet."""
    return {"TagSet": tag_list()}


def model_arn(model_id: str, region: str) -> str:
    """ARN for a foundation model or inference profile.

    Inference-profile IDs are region-prefixed (`us.`, `global.`) and live under a
    different resource type than on-demand foundation models.
    """
    acct = account_id()
    if model_id.startswith(config.INFERENCE_PROFILE_PREFIXES):
        return f"arn:aws:bedrock:{region}:{acct}:inference-profile/{model_id}"
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


# --- tiny JSON state file ----------------------------------------------------
# Records created resource IDs so `ingest`, `query`, and `eval` can find the KB
# that `provision` built without the user pasting IDs around.


def _read_state() -> dict[str, Any]:
    path = Path(config.STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def get_state(experiment_key: str) -> dict[str, Any]:
    return _read_state().get(experiment_key, {})


def save_state(experiment_key: str, **values: Any) -> dict[str, Any]:
    state = _read_state()
    entry = state.setdefault(experiment_key, {})
    entry.update({k: v for k, v in values.items() if v is not None})
    Path(config.STATE_FILE).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return entry


def all_state() -> dict[str, Any]:
    return _read_state()
