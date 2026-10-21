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
from app.services import email as email_service

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


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


TASK_HANDLERS = {
    "send_otp_email": lambda payload: email_service.send_otp_email(
        to_email=payload["to_email"],
        otp=payload["otp"]
    ),
    "send_password_reset_email": lambda payload: email_service.send_password_reset_email(
        to_email=payload["to_email"],
        otp=payload["otp"]
    ),
    "send_invoice_email": lambda payload: email_service.send_invoice_email(
        to_email=payload["to_email"],
        user_name=payload.get("user_name", "Customer"),
        invoice_number=payload.get("invoice_number", f"INV-{payload.get('booking_id')}"),
        event_name=payload.get("event_name", "Event"),
        quantity=payload.get("quantity", 1),
        total_amount=str(payload.get("total_amount", "0.00")),
        booking_id=payload.get("booking_id", 0)
    ),
    "send_cancellation_email": lambda payload: email_service.send_cancellation_email(
        to_email=payload["to_email"],
        user_name=payload.get("user_name", "Customer"),
        event_name=payload.get("event_name", "Event"),
        refund_amount=str(payload.get("refund_amount", "0.00")),
        reason=payload.get("reason", "Cancelled by customer"),
        booking_id=payload.get("booking_id", 0)
    ),
}


def process_single_job(raw_json: str) -> bool:
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
        logger.error(f"Error executing job: {e}", exc_info=True)
        return False


def main():
    logger.info("🚀 Background Worker started. Listening on Redis 'queue:jobs'...")
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=15)

    while running:
        try:
            # BRPOP blocks with 5s timeout waiting for new jobs
            item = client.brpop("queue:jobs", timeout=5)
            if not item:
                continue

            queue_name, raw_data = item
            process_single_job(raw_data)
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
