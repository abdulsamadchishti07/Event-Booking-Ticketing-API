import json
import logging
from typing import Any, Dict, Optional

import redis as syncredis

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

QUEUE_NAME = "queue:jobs"


def enqueue_job_sync(
    task_name: str,
    payload: Dict[str, Any],
    background_tasks: Optional[Any] = None
) -> bool:
    """
    Synchronous enqueue helper: pushes task payload to Redis list 'queue:jobs' in <1ms.
    Safe for sync FastAPI route handlers; falls back to in-memory BackgroundTasks if Redis is unreachable.
    """
    try:
        client = syncredis.Redis.from_url(settings.redis_url, decode_responses=True)
        job = json.dumps({"task": task_name, "payload": payload})
        client.rpush(QUEUE_NAME, job)
        client.close()
        return True
    except Exception as e:
        logger.warning(f"Redis queue unavailable for '{task_name}', falling back to background_tasks: {e}")
        if background_tasks:
            from app.workers.worker import TASK_HANDLERS
            handler = TASK_HANDLERS.get(task_name)
            if handler:
                background_tasks.add_task(handler, payload)
        return False


async def enqueue_job(
    task_name: str,
    payload: Dict[str, Any],
    background_tasks: Optional[Any] = None
) -> bool:
    """
    Asynchronous enqueue helper: pushes task payload to Redis list 'queue:jobs' in <1ms.
    Safe for async FastAPI route handlers; falls back to in-memory BackgroundTasks if Redis is unreachable.
    """
    try:
        client = await get_redis()
        job = json.dumps({"task": task_name, "payload": payload})
        await client.rpush(QUEUE_NAME, job)
        return True
    except Exception as e:
        logger.warning(f"Redis queue unavailable for '{task_name}', falling back to background_tasks: {e}")
        if background_tasks:
            from app.workers.worker import TASK_HANDLERS
            handler = TASK_HANDLERS.get(task_name)
            if handler:
                background_tasks.add_task(handler, payload)
        return False
