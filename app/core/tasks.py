import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
import stripe

from app.database import SessionLocal, model
from app.core.cache import invalidate_services_cache_sync
from app.core.config import settings

logger = logging.getLogger(__name__)


def released_expired_holds(db: Session = None):
    """
    Finds PENDING bookings older than HOLD_DURATION_MINUTES, cancels them,
    returns any assigned seats to AVAILABLE status, and cancels active Stripe PaymentIntents.
    Acquires pessimistic locks on candidate bookings to prevent races with webhook confirmation.
    """
    close_session = (db is None)
    db = db or SessionLocal()

    try:
        now = datetime.now(timezone.utc)
        expire_threshold = now - timedelta(minutes=settings.HOLD_DURATION_MINUTES)

        # 1. Query candidate IDs first
        candidate_ids = [
            row[0] for row in db.query(model.Booking.id).filter(
                model.Booking.status == model.BookingStatus.PENDING,
                model.Booking.created_at <= expire_threshold
            ).all()
        ]

        affected_service_ids = set()
        # 2. Process each booking with pessimistic row lock and rechecking
        for b_id in candidate_ids:
            booking = db.query(model.Booking).filter(
                model.Booking.id == b_id
            ).with_for_update().first()

            # Recheck status and expiry inside locked transaction
            if not booking or booking.status != model.BookingStatus.PENDING:
                continue

            booking_created = booking.created_at
            if booking_created.tzinfo is None:
                booking_created = booking_created.replace(tzinfo=timezone.utc)
            if booking_created > expire_threshold:
                continue

            # Cancel Stripe PaymentIntent if one exists and is pending/active
            if booking.payment and booking.payment.stripe_payment_intent_id:
                if (
                    not booking.payment.stripe_payment_intent_id.startswith("free_")
                    and booking.payment.status != model.PaymentStatus.CANCELED
                ):
                    if booking.payment.status == model.PaymentStatus.SUCCEEDED:
                        # Paid booking must not be cancelled
                        continue

                    try:
                        stripe.PaymentIntent.cancel(booking.payment.stripe_payment_intent_id)
                        booking.payment.status = model.PaymentStatus.CANCELED
                    except stripe.error.InvalidRequestError as e:
                        # Check status with retrieve() instead of matching text
                        is_canceled = False
                        try:
                            pi = stripe.PaymentIntent.retrieve(booking.payment.stripe_payment_intent_id)
                            if getattr(pi, "status", None) == "canceled":
                                is_canceled = True
                            elif getattr(pi, "status", None) == "succeeded":
                                logger.info(
                                    f"PaymentIntent {booking.payment.stripe_payment_intent_id} actually succeeded. Confirming booking."
                                )
                                from app.routers.payments import confirm_booking_and_invoice
                                confirm_booking_and_invoice(db, booking, booking.payment)
                                affected_service_ids.add(booking.service_id)
                                continue
                            else:
                                continue
                        except Exception:
                            pass

                        if is_canceled:
                            booking.payment.status = model.PaymentStatus.CANCELED
                        else:
                            logger.warning(
                                f"Cannot cancel Stripe intent {booking.payment.stripe_payment_intent_id}: {e}. Skipping booking."
                            )
                            continue
                    except Exception as e:
                        logger.warning(
                            f"Failed to cancel Stripe PaymentIntent {booking.payment.stripe_payment_intent_id}: {e}. Skipping booking."
                        )
                        continue

            booking.status = model.BookingStatus.CANCELLED

            for seat in booking.assigned_units:
                seat.status = model.ItemStatus.AVAILABLE

            affected_service_ids.add(booking.service_id)
            db.commit()

        for sid in affected_service_ids:
            invalidate_services_cache_sync(service_id=sid)

    except Exception as e:
        logger.error(f"Error releasing expired holds: {e}", exc_info=True)
        if not close_session:
            db.rollback()
    finally:
        if close_session:
            db.close()


async def run_hold_sweeper_loop():
    """
    Infinite background loop running every 60 seconds.
    Coordinates across multiple Uvicorn workers using a Redis distributed lock.
    Catches all exceptions so sweeper never dies on transient errors.
    """
    import redis.asyncio as aioredis
    import uuid

    worker_id = str(uuid.uuid4())
    lock_key = "lock:hold_sweeper_leader"

    while True:
        try:
            await asyncio.sleep(60)
            redis = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                # Attempt to acquire leader lock for 50s so only 1 worker sweeps per interval
                acquired = await redis.set(lock_key, worker_id, nx=True, ex=50)
                if acquired:
                    await asyncio.to_thread(released_expired_holds)
                else:
                    logger.debug("Hold sweeper: Another worker holds the leader lock. Skipping cycle.")
            finally:
                await redis.aclose()
        except asyncio.CancelledError:
            logger.info("Hold sweeper loop cancelled")
            break
        except Exception as e:
            logger.error(f"Unexpected error in hold sweeper loop: {e}", exc_info=True)