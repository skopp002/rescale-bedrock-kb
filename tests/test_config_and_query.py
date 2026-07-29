"""Tests for experiment wiring, metadata sidecars, and retrieval config.

These guard the two places where a silent mistake is expensive: the metadata
that makes the split corpus addressable as one document, and the per-KB-type
branching in the retrieval API (which fails asynchronously, not at call time).
"""

from __future__ import annotations

import os

import pytest

from rescale_bedrock_kb import config, query, upload
from rescale_bedrock_kb.config import EXPERIMENTS, experiment
from rescale_bedrock_kb.split import Part


@pytest.fixture
def part():
    return Part(
        filename="getting-started__p00100-00189.pdf",
        chapter="Getting Started",
        part_index=1,
        part_count=1,
        start_page=99,
        end_page=188,
        pages=90,
    )


# --- experiment definitions --------------------------------------------------


def test_nova_experiment_pinned_to_us_east_1():
    """Nova multimodal embeddings exist only in us-east-1; pinning it wrong
    would fail at CreateKnowledgeBase."""
    assert EXPERIMENTS["nova"].region == "us-east-1"
    assert "nova-2-multimodal-embeddings" in EXPERIMENTS["nova"].embedding_model


def test_vector_experiments_use_fm_parser():
    for key in ("titan", "nova"):
        exp = EXPERIMENTS[key]
        assert exp.kb_type == "VECTOR"
        assert exp.parsing_strategy == "BEDROCK_FOUNDATION_MODEL"
        assert exp.parser_model
        assert exp.uses_s3_vectors


def test_managed_experiment_owns_nothing():
    exp = EXPERIMENTS["managed"]
    assert exp.kb_type == "MANAGED"
    # AWS supplies the embedder and the vector store for a managed KB.
    assert exp.embedding_model is None
    assert exp.uses_s3_vectors is False


def test_supplemental_bucket_includes_account():
    name = EXPERIMENTS["titan"].supplemental_bucket("123456789012")
    assert "123456789012" in name
    # An empty account segment produced an invalid "--" bucket name once.
    assert "--" not in name


def test_unknown_experiment_exits():
    with pytest.raises(SystemExit):
        experiment("does-not-exist")


# --- config strictness -------------------------------------------------------


def test_judge_differs_from_generation_model():
    """A model must not grade its own answers: the errors it makes are the ones
    it cannot see, so self-grading inflates answer_correctness."""
    assert config.JUDGE_MODEL != config.GENERATION_MODEL


def test_bedrock_reserved_keys_are_non_filterable():
    """S3 Vectors caps filterable metadata at 2 KB per vector. Bedrock's chunk
    text and document-metadata blob each exceed that, and omitting either one
    fails every PutVectors call during ingestion."""
    assert set(config.NON_FILTERABLE_METADATA_KEYS) >= {
        "AMAZON_BEDROCK_TEXT",
        "AMAZON_BEDROCK_METADATA",
    }


def test_judge_omits_deprecated_temperature():
    """Claude 4.6+ rejects `temperature` with a ValidationException, so a null
    in config must drop the key rather than send None."""
    assert "temperature" not in config.JUDGE_INFERENCE.as_config()
    assert config.JUDGE_INFERENCE.as_config()["maxTokens"] == config.JUDGE_INFERENCE.max_tokens


def test_missing_key_raises_config_error(tmp_path):
    """The whole point of the YAML is that an unstated setting fails at build
    time instead of silently defaulting.

    Run in a subprocess rather than reloading the module: the failure we care
    about happens at *import*, and reloading in-process would both hand other
    tests a half-initialised module and defeat `pytest.raises` (a reload defines
    a new ConfigError class that isn't the one imported here).
    """
    import subprocess
    import sys

    incomplete = tmp_path / "config.yaml"
    incomplete.write_text("aws:\n  primary_region: us-west-2\n")
    proc = subprocess.run(
        [sys.executable, "-c", "import rescale_bedrock_kb.config"],
        env={**os.environ, "KB_CONFIG": str(incomplete)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "ConfigError" in proc.stderr
    assert "missing required key 'aws.account_id'" in proc.stderr


# --- upload metadata ---------------------------------------------------------


def test_metadata_carries_provenance_as_one_based_pages(part):
    attrs = upload.metadata_for(part)["metadataAttributes"]
    assert attrs["chapter"]["value"]["stringValue"] == "Getting Started"
    assert attrs["start_page"]["value"]["numberValue"] == 100
    assert attrs["end_page"]["value"]["numberValue"] == 189
    # source_pdf is what re-unifies the split parts into one logical document.
    assert attrs["source_pdf"]["value"]["stringValue"].endswith(".pdf")


def test_only_chapter_is_embedded(part):
    """Page numbers as embedding text would pollute the vectors; the chapter
    title is genuinely useful semantic context."""
    attrs = upload.metadata_for(part)["metadataAttributes"]
    assert attrs["chapter"]["includeForEmbedding"] is True
    for key in ("start_page", "end_page", "pages", "source_pdf", "part"):
        assert attrs[key]["includeForEmbedding"] is False


# --- retrieval filters -------------------------------------------------------


def test_build_filter_returns_bare_clause_when_single():
    """andAll requires two or more clauses, so a lone clause must be bare."""
    f = query.build_filter(chapter="Getting Started")
    assert f == {"equals": {"key": "chapter", "value": "Getting Started"}}


def test_build_filter_page_window_matches_overlap():
    f = query.build_filter(page_from=7763, page_to=7997)
    assert f == {
        "andAll": [
            {"greaterThanOrEquals": {"key": "end_page", "value": 7763}},
            {"lessThanOrEquals": {"key": "start_page", "value": 7997}},
        ]
    }


def test_build_filter_none_when_unconstrained():
    assert query.build_filter() is None


def test_retrieval_config_branches_on_kb_type():
    vector = query._retrieval_config(EXPERIMENTS["titan"], 5, None, "HYBRID")
    assert "vectorSearchConfiguration" in vector
    assert vector["vectorSearchConfiguration"]["overrideSearchType"] == "HYBRID"

    # A managed KB rejects vectorSearchConfiguration outright, and has no
    # overrideSearchType because AWS owns the search strategy.
    managed = query._retrieval_config(EXPERIMENTS["managed"], 5, None, "HYBRID")
    assert "managedSearchConfiguration" in managed
    assert "overrideSearchType" not in managed["managedSearchConfiguration"]


def test_retrieval_config_passes_filter_for_both_types():
    f = query.build_filter(chapter="Design Manager")
    for key, wrapper in (
        ("titan", "vectorSearchConfiguration"),
        ("managed", "managedSearchConfiguration"),
    ):
        cfg = query._retrieval_config(EXPERIMENTS[key], 3, f, None)
        assert cfg[wrapper]["filter"] == f
        assert cfg[wrapper]["numberOfResults"] == 3


# --- chunk parsing -----------------------------------------------------------


def test_chunk_cite_prefers_chapter_and_pages():
    c = query._to_chunk(
        {
            "content": {"text": "body"},
            "score": 0.9,
            "location": {"s3Location": {"uri": "s3://b/corpus/x.pdf"}},
            "metadata": {"chapter": "Design Manager", "start_page": 7763, "end_page": 7997},
        }
    )
    assert c.cite() == "Design Manager (guide pp. 7763-7997)"


def test_chunk_handles_float_page_metadata():
    """Numeric metadata comes back as floats through JSON."""
    c = query._to_chunk({"content": {"text": "t"}, "metadata": {"start_page": 100.0}})
    assert c.start_page == 100


def test_chunk_labels_image_content():
    c = query._to_chunk({"content": {"type": "IMAGE"}, "metadata": {}})
    assert c.text == "[image content]"


# --- cost-attribution tags ---------------------------------------------------


def test_project_name_tag_is_configured():
    """The cost-attribution key the customer reports on. Renaming it silently
    would orphan the historical cost data under the old key."""
    assert config.TAGS["project_name"] == "rescale-kb-evals"


def test_tag_shapes_agree():
    """Three AWS APIs want three shapes for the same tags; a converter that
    disagreed would tag some resources and not others."""
    from rescale_bedrock_kb.aws import tag_list, tag_map, tag_set

    assert tag_map() == config.TAGS
    assert tag_list() == [{"Key": k, "Value": v} for k, v in config.TAGS.items()]
    assert tag_set() == {"TagSet": tag_list()}
    # Same key/value content regardless of shape.
    assert {t["Key"]: t["Value"] for t in tag_list()} == tag_map()


def test_tag_map_is_a_copy():
    """Callers pass these straight into boto3; a shared dict would let one call
    mutate the config for every later one."""
    from rescale_bedrock_kb.aws import tag_map

    tag_map()["project_name"] = "mutated"
    assert config.TAGS["project_name"] == "rescale-kb-evals"
