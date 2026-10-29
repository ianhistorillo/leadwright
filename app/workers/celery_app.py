"""Celery application configuration."""

from __future__ import annotations

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "employee_discovery",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # Useful for local/dev/tests without a live Redis worker interaction.
    task_always_eager=settings.app_env in {"test", "testing"},
    task_eager_propagates=True,
)

# Ensure task modules are registered.
celery_app.autodiscover_tasks(["app.workers"])
