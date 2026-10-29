"""Celery tasks for background job processing."""

from __future__ import annotations

import asyncio
from typing import Any

from app.database.session import get_session_factory
from app.services.job_service import JobService
from app.utils.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


def _run_async(coro):  # noqa: ANN001
    return asyncio.run(coro)


@celery_app.task(name="app.workers.tasks.run_job", bind=True)
def run_job(self, job_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ANN001
    """
    Process a discovery job in controlled batches until complete, paused, or cancelled.

    HTTP handlers only enqueue this task — they never run the full pipeline inline.
    """
    options = options or {}
    logger.info(
        "celery_run_job_started",
        extra={"job_id": job_id, "task_id": getattr(self.request, "id", None)},
    )

    session = get_session_factory()()
    processed_total = 0
    try:
        service = JobService(session)
        while True:
            summary = _run_async(service.process_next_batch(job_id, options=options))
            processed_total += int(summary.get("processed") or 0)
            if summary.get("stopped"):
                logger.info(
                    "celery_run_job_stopped",
                    extra={
                        "job_id": job_id,
                        "status": summary.get("status"),
                        "processed_total": processed_total,
                    },
                )
                return {
                    "job_id": job_id,
                    "status": summary.get("status"),
                    "processed_total": processed_total,
                }
    finally:
        session.close()
