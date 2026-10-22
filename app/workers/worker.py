"""
Standalone asynchronous background job worker.
Continuously pops jobs from Redis list 'queue:jobs' using blocking BRPOP.
Executes slow tasks (emails, invoices, notifications) out-of-band with automatic retries.
"""

import json
import logging
import signal
import sys
import time

import redis

from app.core.config import settings
from app.workers.tasks import TASK_HANDLERS

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s [%(name)s]: %(message)s"
)
logger = logging.getLogger("worker")

running = True


def handle_shutdown(signum, frame):
    global running
    logger.info(f"Signal {signum} received. Finishing current job and stopping cleanly...")
    running = False


def process_single_job(client: redis.Redis, raw_json: str) -> bool:
    job = None
    task_name = "unknown"
    try:
        job = json.loads(raw_json)
        task_name = job.get("task")
        payload = job.get("payload", {})

        handler = TASK_HANDLERS.get(task_name)
        if not handler:
            logger.warning(f"Unknown task name '{task_name}'. Discarding job.")
            return False

        logger.info(f"Processing '{task_name}' for {payload.get('to_email')}")
        handler(payload)
        logger.info(f"Completed '{task_name}' successfully.")
        return True
    except Exception as e:
        logger.error(f"Error executing job '{task_name}': {e}", exc_info=True)
        retries = (job.get("retries", 0) + 1) if isinstance(job, dict) else 99
        if retries <= 3 and isinstance(job, dict):
            job["retries"] = retries
            logger.warning(f"Re-queueing job '{task_name}' (attempt {retries}/3)...")
            client.rpush("queue:jobs", json.dumps(job))
        else:
            logger.error(f"Job '{task_name}' failed after maximum retries. Moving to 'queue:jobs:dead_letter'.")
            client.rpush("queue:jobs:dead_letter", raw_json)
        return False


def main():
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    logger.info("🚀 Background Worker started. Listening on Redis 'queue:jobs'...")
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=15)

    while running:
        try:
            # BRPOP blocks with 5s timeout waiting for new jobs
            item = client.brpop("queue:jobs", timeout=5)
            if not item:
                continue

            queue_name, raw_data = item
            process_single_job(client, raw_data)
        except (redis.exceptions.TimeoutError, TimeoutError):
            # Normal idle timeout when no jobs are present in queue
            continue
        except redis.ConnectionError as e:
            logger.warning(f"Redis connection issue: {e}. Retrying in 2 seconds...")
            time.sleep(2)
        except Exception as e:
            logger.error(f"Unexpected worker loop exception: {e}", exc_info=True)
            time.sleep(1)

    logger.info("Worker stopped cleanly.")


if __name__ == "__main__":
    main()
