import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
import stripe

from app.database import SessionLocal, model
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
                        # If already canceled on Stripe, mark CANCELED and proceed safely
                        if "already" in str(e).lower() or "canceled" in str(e).lower():
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

            db.commit()

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
    Catches all exceptions so sweeper never dies on transient errors.
    """
    while True:
        try:
            await asyncio.sleep(60)
            await asyncio.to_thread(released_expired_holds)
        except asyncio.CancelledError:
            logger.info("Hold sweeper loop cancelled")
            break
        except Exception as e:
            logger.error(f"Unexpected error in hold sweeper loop: {e}", exc_info=True)