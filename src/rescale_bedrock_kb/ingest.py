"""Start and monitor knowledge-base ingestion jobs."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .aws import client
from .config import Experiment

TERMINAL = {"COMPLETE", "FAILED", "STOPPED"}


@dataclass
class JobStatus:
    job_id: str
    status: str
    statistics: dict
    failure_reasons: list[str]

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def summary(self) -> str:
        s = self.statistics or {}
        return (
            f"scanned={s.get('numberOfDocumentsScanned', 0)} "
            f"indexed={s.get('numberOfNewDocumentsIndexed', 0)} "
            f"modified={s.get('numberOfModifiedDocumentsIndexed', 0)} "
            f"deleted={s.get('numberOfDocumentsDeleted', 0)} "
            f"failed={s.get('numberOfDocumentsFailed', 0)}"
        )


def start(exp: Experiment, kb_id: str, ds_id: str) -> str:
    agent = client("bedrock-agent", exp.region)
    job = agent.start_ingestion_job(
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id,
        description=f"Ingest split STAR-CCM+ corpus for {exp.key}",
    )["ingestionJob"]
    return job["ingestionJobId"]


def status(exp: Experiment, kb_id: str, ds_id: str, job_id: str) -> JobStatus:
    agent = client("bedrock-agent", exp.region)
    job = agent.get_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
    )["ingestionJob"]
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        statistics=job.get("statistics", {}),
        failure_reasons=job.get("failureReasons", []) or [],
    )


def watch(exp: Experiment, kb_id: str, ds_id: str, job_id: str, poll: int = 20, timeout: int = 14400):
    """Poll until the job reaches a terminal state, yielding each JobStatus.

    FM parsing a few hundred pages takes a while, so the default ceiling is
    generous -- it's a safety net, not an expectation.
    """
    deadline = time.monotonic() + timeout
    while True:
        st = status(exp, kb_id, ds_id, job_id)
        yield st
        if st.done:
            return
        if time.monotonic() >= deadline:
            raise SystemExit(f"Ingestion job {job_id} still {st.status} after {timeout}s")
        time.sleep(poll)


def latest_job(exp: Experiment, kb_id: str, ds_id: str) -> JobStatus | None:
    agent = client("bedrock-agent", exp.region)
    jobs = agent.list_ingestion_jobs(
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        maxResults=1,
    ).get("ingestionJobSummaries", [])
    if not jobs:
        return None
    j = jobs[0]
    return JobStatus(
        job_id=j["ingestionJobId"],
        status=j["status"],
        statistics=j.get("statistics", {}),
        failure_reasons=[],
    )
