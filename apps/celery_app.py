from __future__ import annotations

import os

from celery import Celery


def create_celery() -> Celery:
    redis_url = os.getenv("SUPERVISOR_REDIS_URL", "redis://127.0.0.1:6379/0")
    app = Celery("supervisor_tasks", broker=redis_url, backend=redis_url, include=["apps.supervisor_tasks"])
    app.conf.update(
        task_track_started=True,
        worker_send_task_events=True,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone=os.getenv("TZ", "UTC"),
        enable_utc=True,
    )
    return app


celery_app = create_celery()
