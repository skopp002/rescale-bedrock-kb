"""Tests for the accuracy metrics.

The scoring logic is what every conclusion in the comparison rests on, so it is
tested independently of AWS.
"""

from __future__ import annotations

from rescale_bedrock_kb import evaluate
from rescale_bedrock_kb.evaluate import EvalQuestion, ExperimentReport, score_retrieval
from rescale_bedrock_kb.query import Chunk


def chunk(start: int | None, end: int | None, name: str = "getting-started__p00100-00189.pdf"):
    return Chunk(
        text="text",
        score=0.5,
        source_uri=f"s3://bucket/corpus/{name}",
        chapter="Getting Started",
        start_page=start,
        end_page=end,
    )


def question(**overrides):
    # EvalQuestion has no field defaults -- an under-specified question is a bug
    # in the eval set -- so the fixture supplies every field explicitly.
    base = dict(
        id="q1",
        question="q?",
        expected_pages=[[100, 189]],
        reference_answer="ref",
        expected_keywords=["ref"],
        chapter="Getting Started",
        kind="text",
    )
    base.update(overrides)
    return EvalQuestion(**base)


def test_relevant_on_overlap_not_containment():
    q = question(expected_pages=[[100, 189]])
    assert q.relevant(chunk(100, 189))  # exact
    assert q.relevant(chunk(50, 120))  # straddles the start
    assert q.relevant(chunk(150, 300))  # straddles the end
    assert q.relevant(chunk(120, 130))  # fully inside
    assert not q.relevant(chunk(190, 250))  # just past the end
    assert not q.relevant(chunk(1, 99))  # just before the start


def test_relevant_falls_back_to_filename_when_metadata_absent():
    """A managed KB may not echo the sidecar attributes, so the page range
    encoded in the object key is the backstop."""
    q = question(expected_pages=[[100, 189]])
    assert q.relevant(chunk(None, None, "getting-started__p00100-00189.pdf"))
    assert not q.relevant(chunk(None, None, "design-manager__p07763-07997.pdf"))
    # Nothing to go on at all -> not relevant, rather than a crash.
    assert not q.relevant(chunk(None, None, "unparseable-name.pdf"))


def test_relevant_across_multiple_expected_ranges():
    q = question(expected_pages=[[100, 189], [13757, 13779]])
    assert q.relevant(chunk(13760, 13770))
    assert not q.relevant(chunk(5000, 5100))


def test_score_retrieval_ranks_first_hit():
    q = question()
    chunks = [chunk(1, 50), chunk(100, 189), chunk(300, 400)]
    score = score_retrieval(q, chunks, latency_ms=12.0)

    assert score.recall_at_k == 1.0
    assert score.reciprocal_rank == 0.5  # hit at rank 2
    assert score.hit_at_1 is False
    assert score.precision_at_k == 1 / 3


def test_score_retrieval_all_miss():
    score = score_retrieval(question(), [chunk(1, 50), chunk(300, 400)], latency_ms=1.0)
    assert score.recall_at_k == 0.0
    assert score.reciprocal_rank == 0.0
    assert score.precision_at_k == 0.0


def test_score_retrieval_handles_empty_results():
    score = score_retrieval(question(), [], latency_ms=1.0)
    assert score.recall_at_k == 0.0
    assert score.reciprocal_rank == 0.0
    assert score.top_citation is None


def test_keyword_coverage_is_case_insensitive():
    assert evaluate._keyword_coverage("Use STARCCM+ -HOST now", ["starccm+", "-host"]) == 1.0
    assert evaluate._keyword_coverage("only one", ["only", "missing"]) == 0.5
    # No keywords declared shouldn't penalise the answer.
    assert evaluate._keyword_coverage("anything", []) == 1.0


def test_aggregate_normalises_judge_score_to_unit_scale():
    report = ExperimentReport(
        experiment="titan",
        label="l",
        region="us-west-2",
        top_k=5,
        retrieval=[
            score_retrieval(question(), [chunk(100, 189)], 10.0),
            score_retrieval(question(), [chunk(1, 2)], 20.0),
        ],
        answers=[
            evaluate.AnswerScore("q1", 3, "", 1.0, True, 100.0, "a"),
            evaluate.AnswerScore("q2", 0, "", 0.0, False, 200.0, "b"),
        ],
    )
    agg = report.aggregate()

    assert agg["recall@5"] == 0.5
    assert agg["mrr"] == 0.5
    assert agg["answer_correctness"] == 0.5  # (3/3 + 0/3) / 2
    assert agg["citation_precision"] == 0.5


def test_by_kind_separates_figure_questions():
    questions = [question(id="q1", kind="text"), question(id="q2", kind="figure")]
    report = ExperimentReport(
        experiment="e",
        label="l",
        region="r",
        top_k=5,
        retrieval=[
            score_retrieval(questions[0], [chunk(100, 189)], 1.0),
            score_retrieval(questions[1], [chunk(1, 2)], 1.0),
        ],
        answers=[],
    )
    assert report.by_kind(questions) == {"figure": 0.0, "text": 1.0}
