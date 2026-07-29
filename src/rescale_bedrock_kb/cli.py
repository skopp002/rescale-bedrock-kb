"""Command line interface.

Each pipeline stage is its own command so the Titan / Nova / managed variants can
be run, re-run, and compared independently against one shared corpus and one
shared eval set:

    split -> upload -> provision -> ingest -> eval -> compare
"""

from __future__ import annotations

from pathlib import Path

import typer
from pypdf import PdfReader
from rich.console import Console
from rich.table import Table

from . import config, evaluate
from . import ingest as ingest_mod
from . import provision as provision_mod
from . import split as split_mod
from . import upload as upload_mod
from .aws import all_state
from .config import EXPERIMENTS, experiment
from .query import build_filter, retrieve, retrieve_and_generate

app = typer.Typer(
    add_completion=False,
    help="Build and compare two Bedrock knowledge bases over the STAR-CCM+ user guide.",
    no_args_is_help=True,
)
console = Console()


def _experiment_keys(keys: list[str] | None) -> list[str]:
    if not keys:
        return list(config.DEFAULT_EXPERIMENTS)
    if len(keys) == 1 and keys[0] == "all":
        return list(EXPERIMENTS)
    return keys


@app.command()
def experiments():
    """List the configured knowledge-base experiments."""
    table = Table(title="Experiments", show_lines=True)
    for col in ("key", "type", "region", "embedding", "parser", "notes"):
        table.add_column(col, overflow="fold")
    for exp in EXPERIMENTS.values():
        table.add_row(
            exp.key,
            exp.kb_type,
            exp.region,
            exp.embedding_model or "AWS-managed",
            f"{exp.parsing_strategy}\n{exp.parser_model or ''}".strip(),
            exp.notes,
        )
    console.print(table)


@app.command()
def split(
    subset: bool = typer.Option(True, help="Only write the subset chapters from config."),
    overwrite: bool = typer.Option(False, help="Rewrite parts that already exist."),
    show_plan: bool = typer.Option(False, "--plan", help="Print the plan and exit."),
):
    """Split the source PDF into per-chapter (and part) PDFs."""
    if not config.SOURCE_PDF.exists():
        raise typer.BadParameter(f"Source PDF not found at {config.SOURCE_PDF}")

    console.print(f"Reading [cyan]{config.SOURCE_PDF.name}[/] ...")
    reader = PdfReader(str(config.SOURCE_PDF))
    parts = split_mod.plan(reader)
    chosen = split_mod.select(parts, subset)

    table = Table(title=f"Split plan ({len(chosen)} parts, {sum(p.pages for p in chosen)} pages)")
    for col in ("file", "chapter", "pages"):
        table.add_column(col, overflow="fold")
    for p in chosen:
        table.add_row(p.filename, p.chapter, f"{p.start_page + 1}-{p.end_page + 1} ({p.pages}p)")
    console.print(table)
    if show_plan:
        return

    written = 0
    with console.status("Writing parts ...") as status:
        for part, did in split_mod.write_parts(chosen, reader, config.SPLIT_DIR, overwrite):
            written += int(did)
            status.update(f"{part.filename} {'written' if did else 'exists'}")
    # The manifest covers every chosen part so upload/eval share one view.
    manifest = split_mod.save_manifest(chosen, config.SPLIT_DIR)
    console.print(f"[green]{written} written[/], {len(chosen) - written} already present")
    console.print(f"Manifest: [cyan]{manifest}[/]")


@app.command()
def upload(
    experiment_keys: list[str] | None = typer.Argument(None, help="Experiment keys, or 'all'."),
    force: bool = typer.Option(False, help="Re-upload even if size matches."),
):
    """Upload the split PDFs (plus metadata sidecars) to S3."""
    parts = split_mod.load_manifest()
    # One bucket per region -- an ingestion job can't read cross-region.
    regions = {experiment(k).region for k in _experiment_keys(experiment_keys)}
    for region in sorted(regions):
        bucket = upload_mod.bucket_name(region)
        console.print(f"\n[bold]{region}[/] -> s3://{bucket}/{upload_mod.CORPUS_PREFIX}")
        count = 0
        with console.status(f"Uploading to {region} ...") as status:
            for part, did in upload_mod.upload_parts(parts, region, force=force):
                count += int(did)
                status.update(f"{part.filename} {'uploaded' if did else 'skipped'}")
        console.print(f"[green]{count} uploaded[/], {len(parts) - count} already current")


@app.command()
def provision(
    experiment_keys: list[str] | None = typer.Argument(None, help="Experiment keys, or 'all'."),
):
    """Create the IAM role, vector store, knowledge base, and data source."""
    for key in _experiment_keys(experiment_keys):
        exp = experiment(key)
        console.print(f"\n[bold]{exp.key}[/] -- {exp.label} ({exp.region})")
        with console.status(f"Provisioning {exp.key} ..."):
            state = provision_mod.provision(exp)
        console.print(f"  knowledge base: [green]{state['knowledge_base_id']}[/]")
        console.print(f"  data source:    [green]{state['data_source_id']}[/]")


@app.command()
def ingest(
    experiment_keys: list[str] | None = typer.Argument(None, help="Experiment keys, or 'all'."),
    wait: bool = typer.Option(True, help="Poll until each job finishes."),
):
    """Start an ingestion job (parse + chunk + embed) for each experiment."""
    for key in _experiment_keys(experiment_keys):
        exp = experiment(key)
        kb_id, ds_id = provision_mod.resolve(exp)
        job_id = ingest_mod.start(exp, kb_id, ds_id)
        console.print(f"\n[bold]{exp.key}[/] ingestion job [cyan]{job_id}[/] started")
        if not wait:
            continue
        with console.status(f"{exp.key}: ingesting ...") as status:
            for st in ingest_mod.watch(exp, kb_id, ds_id, job_id):
                status.update(f"{exp.key}: {st.status} -- {st.summary()}")
        colour = "green" if st.status == "COMPLETE" else "red"
        console.print(f"  [{colour}]{st.status}[/] -- {st.summary()}")
        for reason in st.failure_reasons:
            console.print(f"  [red]{reason}[/]")


@app.command()
def status():
    """Show provisioned resources and the latest ingestion job for each."""
    state = all_state()
    if not state:
        console.print("Nothing provisioned yet.")
        return
    table = Table(title="Status", show_lines=True)
    for col in ("experiment", "region", "knowledge base", "last ingestion"):
        table.add_column(col, overflow="fold")
    for key, entry in sorted(state.items()):
        exp = experiment(key)
        job = "-"
        kb_id, ds_id = entry.get("knowledge_base_id"), entry.get("data_source_id")
        if kb_id and ds_id:
            try:
                latest = ingest_mod.latest_job(exp, kb_id, ds_id)
                job = f"{latest.status}\n{latest.summary()}" if latest else "none"
            except Exception as exc:  # noqa: BLE001 - status should never hard-fail
                job = f"[red]{type(exc).__name__}[/]"
        table.add_row(key, entry.get("region", "?"), kb_id or "?", job)
    console.print(table)


@app.command()
def ask(
    question: str,
    experiment_key: str = typer.Option(..., "--experiment", "-e", help="Experiment key."),
    top_k: int = typer.Option(config.TOP_K, "--top-k", "-k"),
    generate: bool = typer.Option(True, help="Generate an answer, not just chunks."),
    chapter: str | None = typer.Option(None, help="Restrict to one chapter (exact title)."),
    page_from: int | None = typer.Option(None, help="Restrict to guide pages >= this."),
    page_to: int | None = typer.Option(None, help="Restrict to guide pages <= this."),
    hybrid: bool = typer.Option(False, help="Force HYBRID search instead of the KB default."),
):
    """Query one knowledge base interactively.

    The corpus is split across many S3 objects, so --chapter and --page-from /
    --page-to are metadata filters that scope a query back to a region of the
    original guide.
    """
    exp = experiment(experiment_key)
    kb_id, _ = provision_mod.resolve(exp)
    metadata_filter = build_filter(chapter=chapter, page_from=page_from, page_to=page_to)
    search_type = "HYBRID" if hybrid else None
    if metadata_filter:
        console.print(f"[dim]filter: {metadata_filter}[/]")

    if generate:
        answer = retrieve_and_generate(
            exp, kb_id, question, top_k, metadata_filter=metadata_filter, search_type=search_type
        )
        console.print(f"\n[bold]{answer.text}[/]\n")
        for i, c in enumerate(answer.citations, 1):
            console.print(f"  [{i}] {c.cite()}")
        return

    results = retrieve(
        exp, kb_id, question, top_k, metadata_filter=metadata_filter, search_type=search_type
    )
    for i, c in enumerate(results, 1):
        score = f"{c.score:.4f}" if c.score is not None else "n/a"
        console.print(f"\n[bold]{i}.[/] score={score} {c.cite()}")
        console.print(f"   {c.text[:400]}")


@app.command("eval")
def eval_cmd(
    experiment_keys: list[str] | None = typer.Argument(None, help="Experiment keys, or 'all'."),
    top_k: int = typer.Option(config.TOP_K, "--top-k", "-k"),
    answers: bool = typer.Option(True, help="Also grade generated answers with an LLM judge."),
    questions_file: Path | None = typer.Option(None, "--questions"),
):
    """Run the accuracy evaluation against one or more knowledge bases."""
    questions = evaluate.load_questions(questions_file)
    console.print(f"Loaded [cyan]{len(questions)}[/] eval questions")

    for key in _experiment_keys(experiment_keys):
        exp = experiment(key)
        kb_id, _ = provision_mod.resolve(exp)
        console.print(f"\n[bold]{exp.key}[/] -- {exp.label}")

        with console.status(f"{exp.key}: evaluating ...") as status:
            def progress(q, r, a, _status=status):
                mark = "[green]hit[/]" if r.recall_at_k else "[red]miss[/]"
                grade = f" judge={a.judge_score}/3" if a else ""
                _status.update(f"{q.id}: {mark} rr={r.reciprocal_rank:.2f}{grade}")

            report = evaluate.run(
                exp, kb_id, questions, top_k=top_k, with_answers=answers, progress=progress
            )

        path = evaluate.save_report(report, questions)
        _print_aggregate(report, questions)
        console.print(f"  saved -> [cyan]{path}[/]")


def _print_aggregate(report: evaluate.ExperimentReport, questions: list[evaluate.EvalQuestion]):
    agg = report.aggregate()
    table = Table(title=f"{report.experiment} results", show_header=False)
    table.add_column("metric")
    table.add_column("value", justify="right")
    for k, v in agg.items():
        table.add_row(k, str(v))
    for kind, val in report.by_kind(questions).items():
        table.add_row(f"recall ({kind} questions)", str(val))
    console.print(table)


@app.command()
def compare():
    """Compare saved evaluation reports side by side."""
    import json

    reports = []
    for path in sorted(config.RESULTS_DIR.glob("*.json")):
        reports.append(json.loads(path.read_text()))
    if not reports:
        raise typer.BadParameter(f"No reports in {config.RESULTS_DIR}. Run `eval` first.")

    metrics: list[str] = []
    for r in reports:
        for k in list(r["aggregate"]) + [f"recall[{x}]" for x in r.get("recall_by_kind", {})]:
            if k not in metrics:
                metrics.append(k)

    table = Table(title="Knowledge base accuracy comparison", show_lines=True)
    table.add_column("metric", overflow="fold")
    for r in reports:
        table.add_column(r["experiment"], justify="right")

    for metric in metrics:
        row = [metric]
        for r in reports:
            if metric.startswith("recall["):
                kind = metric[len("recall[") : -1]
                val = r.get("recall_by_kind", {}).get(kind)
            else:
                val = r["aggregate"].get(metric)
            row.append("-" if val is None else str(val))
        table.add_row(*row)
    console.print(table)

    for r in reports:
        console.print(f"[dim]{r['experiment']}: {r['label']} ({r['region']})[/]")


if __name__ == "__main__":
    app()
