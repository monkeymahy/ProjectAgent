from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "projshow",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_track_started=True,
    task_time_limit=settings.clone_timeout + 300,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

import app.tasks  # noqa: F401,E402  (register tasks)
