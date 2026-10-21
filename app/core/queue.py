import json
import logging
from typing import Any, Dict, Optional

import redis as syncredis

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

QUEUE_NAME = "queue:jobs"


_sync_client = syncredis.Redis.from_url(settings.redis_url, decode_responses=True)


def _fallback(task_name: str, payload: Dict[str, Any], background_tasks: Optional[Any]):
    if background_tasks:
        from app.workers.tasks import TASK_HANDLERS
        handler = TASK_HANDLERS.get(task_name)
        if handler:
            background_tasks.add_task(handler, payload)


def enqueue_job_sync(
    task_name: str,
    payload: Dict[str, Any],
    background_tasks: Optional[Any] = None
) -> bool:
    try:
        _sync_client.rpush(QUEUE_NAME, json.dumps({"task": task_name, "payload": payload}))
        return True
    except Exception as e:
        logger.warning(f"Redis queue unavailable for '{task_name}', falling back to background_tasks: {e}")
        _fallback(task_name, payload, background_tasks)
        return False


async def enqueue_job(
    task_name: str,
    payload: Dict[str, Any],
    background_tasks: Optional[Any] = None
) -> bool:
    try:
        client = await get_redis()
        await client.rpush(QUEUE_NAME, json.dumps({"task": task_name, "payload": payload}))
        return True
    except Exception as e:
        logger.warning(f"Redis queue unavailable for '{task_name}', falling back to background_tasks: {e}")
        _fallback(task_name, payload, background_tasks)
        return False
