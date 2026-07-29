"""Accuracy evaluation shared by every knowledge base.

Three families of metric, because retrieval quality and answer quality fail
independently:

  Retrieval  -- recall@k, precision@k, MRR, and hit@1 scored against the
      *source page ranges* in the eval set. A chunk counts as relevant when its
      page range overlaps an expected range, which is robust to differing
      chunk boundaries between experiments (the managed KB chunks differently
      from ours, so chunk-id matching would be meaningless).
  Answer     -- an LLM judge grades the generated answer against a reference
      answer on a 0-3 scale, plus a keyword-based groundedness floor.
  Operational-- latency per query, so an accuracy win can be weighed against
      its cost in responsiveness.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config
from .aws import client, model_arn
from .config import Experiment
from .query import Answer, Chunk, retrieve, retrieve_and_generate


@dataclass
class EvalQuestion:
    """One ground-truth item.

    `expected_pages` are 1-based inclusive [start, end] ranges in the original
    14,125-page guide -- the same numbering carried in the upload metadata.
    """

    # No defaults: every question in the eval set must state its keywords,
    # chapter, and kind, so a silently-empty keyword list can't quietly score a
    # perfect 1.0 on keyword coverage.
    id: str
    question: str
    expected_pages: list[list[int]]
    reference_answer: str
    expected_keywords: list[str]
    chapter: str
    kind: str  # "text" | "figure" | "table" -- figure/table probe multimodal parsing

    def relevant(self, chunk: Chunk) -> bool:
        if chunk.start_page is None or chunk.end_page is None:
            # Managed KBs may not echo our sidecar metadata; fall back to
            # matching the page range encoded in the object key.
            lo, hi = _pages_from_filename(chunk.filename)
            if lo is None:
                return False
        else:
            lo, hi = chunk.start_page, chunk.end_page
        return any(lo <= exp_hi and hi >= exp_lo for exp_lo, exp_hi in self.expected_pages)


_PAGE_RE = re.compile(r"__p(\d+)-(\d+)\.pdf$")


def _pages_from_filename(name: str | None) -> tuple[int | None, int | None]:
    if not name:
        return None, None
    m = _PAGE_RE.search(name)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def load_questions(path: Path | None = None) -> list[EvalQuestion]:
    path = path or config.EVAL_QUESTIONS
    if not path.exists():
        raise SystemExit(f"No eval set at {path}.")
    data = json.loads(path.read_text())
    try:
        return [EvalQuestion(**q) for q in data["questions"]]
    except TypeError as exc:
        # Same principle as config: an under-specified question is a bug in the
        # eval set, not something to paper over with a default.
        raise SystemExit(f"{path}: malformed eval question -- {exc}") from None


# --- retrieval metrics -------------------------------------------------------


@dataclass
class RetrievalScore:
    question_id: str
    hits: list[bool]
    recall_at_k: float
    precision_at_k: float
    reciprocal_rank: float
    hit_at_1: bool
    latency_ms: float
    top_citation: str | None


def score_retrieval(q: EvalQuestion, chunks: list[Chunk], latency_ms: float) -> RetrievalScore:
    hits = [q.relevant(c) for c in chunks]
    first = next((i for i, h in enumerate(hits) if h), None)
    return RetrievalScore(
        question_id=q.id,
        hits=hits,
        # "Did we surface the right source at all?" -- the expected ranges are
        # regions, not a countable set of gold chunks, so recall is binary here.
        recall_at_k=1.0 if any(hits) else 0.0,
        precision_at_k=(sum(hits) / len(hits)) if hits else 0.0,
        reciprocal_rank=(1.0 / (first + 1)) if first is not None else 0.0,
        hit_at_1=bool(hits and hits[0]),
        latency_ms=latency_ms,
        top_citation=chunks[0].cite() if chunks else None,
    )


# --- answer metrics ----------------------------------------------------------

JUDGE_PROMPT = """You are grading a question-answering system that reads a \
Simcenter STAR-CCM+ user guide.

Question:
{question}

Reference answer (ground truth):
{reference}

System answer:
{candidate}

Grade the system answer for factual correctness against the reference. Ignore \
differences in wording, length, and formatting. Judge only whether the \
substance is right.

Score on this scale:
3 = fully correct and complete
2 = correct but missing a secondary detail
1 = partially correct, or correct but with a material omission
0 = incorrect, contradicts the reference, or declines to answer

Respond with only a JSON object, no other text:
{{"score": <0-3>, "reason": "<one sentence>"}}"""


@dataclass
class AnswerScore:
    question_id: str
    judge_score: int
    judge_reason: str
    keyword_coverage: float
    cited_correct_source: bool
    latency_ms: float
    answer_text: str


def _judge(question: EvalQuestion, candidate: str, region: str) -> tuple[int, str]:
    if not candidate.strip():
        return 0, "empty answer"
    runtime = client("bedrock-runtime", region)
    prompt = JUDGE_PROMPT.format(
        question=question.question,
        reference=question.reference_answer,
        candidate=candidate,
    )
    resp = runtime.converse(
        modelId=model_arn(config.JUDGE_MODEL, region),
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=config.JUDGE_INFERENCE.as_config(),
    )
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return 0, f"unparseable judge response: {text[:120]}"
    try:
        parsed = json.loads(match.group(0))
        return int(parsed.get("score", 0)), str(parsed.get("reason", ""))[:300]
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0, f"unparseable judge response: {text[:120]}"


def _keyword_coverage(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    low = answer.lower()
    return sum(1 for kw in keywords if kw.lower() in low) / len(keywords)


def score_answer(
    q: EvalQuestion, answer: Answer, latency_ms: float, region: str
) -> AnswerScore:
    score, reason = _judge(q, answer.text, region)
    return AnswerScore(
        question_id=q.id,
        judge_score=score,
        judge_reason=reason,
        keyword_coverage=_keyword_coverage(answer.text, q.expected_keywords),
        cited_correct_source=any(q.relevant(c) for c in answer.citations),
        latency_ms=latency_ms,
        answer_text=answer.text,
    )


# --- runner ------------------------------------------------------------------


@dataclass
class ExperimentReport:
    experiment: str
    label: str
    region: str
    top_k: int
    retrieval: list[RetrievalScore]
    answers: list[AnswerScore]

    def aggregate(self) -> dict:
        def mean(vals):
            vals = list(vals)
            return round(statistics.fmean(vals), 4) if vals else 0.0

        agg = {
            "questions": len(self.retrieval),
            f"recall@{self.top_k}": mean(r.recall_at_k for r in self.retrieval),
            f"precision@{self.top_k}": mean(r.precision_at_k for r in self.retrieval),
            "mrr": mean(r.reciprocal_rank for r in self.retrieval),
            "hit@1": mean(1.0 if r.hit_at_1 else 0.0 for r in self.retrieval),
            "retrieve_latency_ms_p50": round(
                statistics.median([r.latency_ms for r in self.retrieval]), 1
            )
            if self.retrieval
            else 0.0,
        }
        if self.answers:
            agg.update(
                {
                    # Normalised so 1.0 is a perfect grade across the set.
                    "answer_correctness": mean(a.judge_score / 3 for a in self.answers),
                    "keyword_coverage": mean(a.keyword_coverage for a in self.answers),
                    "citation_precision": mean(
                        1.0 if a.cited_correct_source else 0.0 for a in self.answers
                    ),
                    "rag_latency_ms_p50": round(
                        statistics.median([a.latency_ms for a in self.answers]), 1
                    ),
                }
            )
        return agg

    def by_kind(self, questions: list[EvalQuestion]) -> dict:
        """Break retrieval down by question kind -- this is where a multimodal
        embedding model should separate from a text-only one."""
        kinds: dict[str, list[float]] = {}
        lookup = {q.id: q.kind for q in questions}
        for r in self.retrieval:
            kinds.setdefault(lookup.get(r.question_id, "text"), []).append(r.recall_at_k)
        return {k: round(statistics.fmean(v), 4) for k, v in sorted(kinds.items()) if v}


def run(
    exp: Experiment,
    kb_id: str,
    questions: list[EvalQuestion],
    top_k: int,
    with_answers: bool,
    progress=None,
) -> ExperimentReport:
    retrieval: list[RetrievalScore] = []
    answers: list[AnswerScore] = []

    for q in questions:
        t0 = time.perf_counter()
        chunks = retrieve(exp, kb_id, q.question, top_k)
        retrieval.append(score_retrieval(q, chunks, (time.perf_counter() - t0) * 1000))

        if with_answers:
            t1 = time.perf_counter()
            answer = retrieve_and_generate(exp, kb_id, q.question, top_k)
            answers.append(
                score_answer(q, answer, (time.perf_counter() - t1) * 1000, exp.region)
            )
        if progress:
            progress(q, retrieval[-1], answers[-1] if answers else None)

    return ExperimentReport(
        experiment=exp.key,
        label=exp.label,
        region=exp.region,
        top_k=top_k,
        retrieval=retrieval,
        answers=answers,
    )


def save_report(report: ExperimentReport, questions: list[EvalQuestion]) -> Path:
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RESULTS_DIR / f"{report.experiment}.json"
    path.write_text(
        json.dumps(
            {
                "experiment": report.experiment,
                "label": report.label,
                "region": report.region,
                "top_k": report.top_k,
                "aggregate": report.aggregate(),
                "recall_by_kind": report.by_kind(questions),
                "retrieval": [asdict(r) for r in report.retrieval],
                "answers": [asdict(a) for a in report.answers],
            },
            indent=2,
        )
        + "\n"
    )
    return path
