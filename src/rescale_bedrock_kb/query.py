"""Retrieval and answer generation against any of the knowledge bases.

The same two calls work for VECTOR and MANAGED knowledge bases -- that
uniformity is what makes the accuracy comparison fair, since only the KB id
changes between experiments.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .aws import client, model_arn
from .config import Experiment


@dataclass
class Chunk:
    """One retrieved chunk, with provenance resolved from sidecar metadata."""

    text: str
    score: float | None
    source_uri: str | None
    chapter: str | None
    start_page: int | None
    end_page: int | None
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def filename(self) -> str | None:
        return self.source_uri.rsplit("/", 1)[-1] if self.source_uri else None

    def cite(self) -> str:
        where = self.chapter or self.filename or "unknown source"
        if self.start_page and self.end_page:
            return f"{where} (guide pp. {self.start_page}-{self.end_page})"
        return where


def _to_chunk(result: dict) -> Chunk:
    meta = result.get("metadata", {}) or {}
    loc = result.get("location", {}) or {}
    uri = None
    for shape in ("s3Location", "customDocumentLocation", "kendraDocumentLocation"):
        if shape in loc:
            uri = loc[shape].get("uri") or loc[shape].get("id")
            break

    def num(key: str) -> int | None:
        val = meta.get(key)
        try:
            return int(float(val)) if val is not None else None
        except (TypeError, ValueError):
            return None

    content = result.get("content", {}) or {}
    text = content.get("text") or ""
    if not text and content.get("type") == "IMAGE":
        text = "[image content]"

    return Chunk(
        text=text,
        score=result.get("score"),
        source_uri=uri,
        chapter=meta.get("chapter"),
        start_page=num("start_page"),
        end_page=num("end_page"),
        raw=result,
    )


def build_filter(
    chapter: str | None = None,
    chapters: list[str] | None = None,
    page_from: int | None = None,
    page_to: int | None = None,
) -> dict | None:
    """Assemble a metadata filter from the attributes the upload sidecars set.

    The corpus is split across many objects, so filters are how you narrow a
    query back down to one logical region of the guide. Page bounds compare
    against the part's own range: `end_page >= page_from` and
    `start_page <= page_to` selects every part that *overlaps* the window,
    rather than only parts fully inside it.
    """
    clauses: list[dict] = []
    if chapter:
        clauses.append({"equals": {"key": "chapter", "value": chapter}})
    if chapters:
        clauses.append({"in": {"key": "chapter", "value": chapters}})
    if page_from is not None:
        clauses.append({"greaterThanOrEquals": {"key": "end_page", "value": page_from}})
    if page_to is not None:
        clauses.append({"lessThanOrEquals": {"key": "start_page", "value": page_to}})

    if not clauses:
        return None
    # A single clause must be passed bare; andAll requires two or more.
    return clauses[0] if len(clauses) == 1 else {"andAll": clauses}


def _retrieval_config(
    exp: Experiment,
    top_k: int,
    metadata_filter: dict | None,
    search_type: str | None,
) -> dict:
    """Retrieval config for this KB flavour.

    Managed knowledge bases reject `vectorSearchConfiguration` and take
    `managedSearchConfiguration` instead -- it accepts the same filter grammar
    but has no `overrideSearchType`, since AWS owns the search strategy.
    """
    cfg: dict = {"numberOfResults": top_k}
    if metadata_filter:
        cfg["filter"] = metadata_filter

    if exp.kb_type == "MANAGED":
        return {"managedSearchConfiguration": cfg}
    if search_type:
        cfg["overrideSearchType"] = search_type
    return {"vectorSearchConfiguration": cfg}


def retrieve(
    exp: Experiment,
    kb_id: str,
    question: str,
    top_k: int,
    metadata_filter: dict | None = None,
    search_type: str | None = None,
) -> list[Chunk]:
    runtime = client("bedrock-agent-runtime", exp.region)
    resp = runtime.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": question},
        retrievalConfiguration=_retrieval_config(exp, top_k, metadata_filter, search_type),
    )
    return [_to_chunk(r) for r in resp.get("retrievalResults", [])]


@dataclass
class Answer:
    text: str
    citations: list[Chunk]


ANSWER_PROMPT = """Answer the question using only the excerpts below, which come \
from the Simcenter STAR-CCM+ user guide. If the excerpts don't contain the \
answer, say so rather than guessing.

Excerpts:
{context}

Question: {question}

Answer:"""


def _generate_from_chunks(
    exp: Experiment, question: str, chunks: list[Chunk], model_id: str
) -> Answer:
    """Retrieve-then-generate, done client side.

    Managed knowledge bases don't support RetrieveAndGenerate, so to compare
    answer quality across all three KBs we generate from the retrieved chunks
    ourselves. Every experiment then gets the same generation model and the same
    prompt, which makes the answer metric a measure of *retrieval* quality
    rather than of differing server-side RAG implementations.
    """
    if not chunks:
        return Answer(text="", citations=[])
    context = "\n\n".join(
        f"[{i}] ({c.cite()})\n{c.text}" for i, c in enumerate(chunks, 1) if c.text
    )
    runtime = client("bedrock-runtime", exp.region)
    resp = runtime.converse(
        modelId=model_arn(model_id, exp.region),
        messages=[
            {
                "role": "user",
                "content": [{"text": ANSWER_PROMPT.format(context=context, question=question)}],
            }
        ],
        inferenceConfig=config.GENERATION_INFERENCE.as_config(),
    )
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    return Answer(text=text, citations=chunks)


def retrieve_and_generate(
    exp: Experiment,
    kb_id: str,
    question: str,
    top_k: int,
    model: str | None = None,
    metadata_filter: dict | None = None,
    search_type: str | None = None,
) -> Answer:
    runtime = client("bedrock-agent-runtime", exp.region)
    model_id = model or config.GENERATION_MODEL

    if exp.kb_type == "MANAGED":
        chunks = retrieve(exp, kb_id, question, top_k, metadata_filter, search_type)
        return _generate_from_chunks(exp, question, chunks, model_id)

    resp = runtime.retrieve_and_generate(
        input={"text": question},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": model_arn(model_id, exp.region),
                "retrievalConfiguration": _retrieval_config(
                    exp, top_k, metadata_filter, search_type
                ),
            },
        },
    )
    chunks: list[Chunk] = []
    for citation in resp.get("citations", []):
        for ref in citation.get("retrievedReferences", []):
            chunks.append(_to_chunk(ref))
    return Answer(text=resp.get("output", {}).get("text", ""), citations=chunks)
