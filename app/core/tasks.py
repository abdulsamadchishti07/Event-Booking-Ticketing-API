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
    """
    close_session = (db is None)
    db = db or SessionLocal()

    try:
        now = datetime.now(timezone.utc)
        expire_threshold = now - timedelta(minutes=settings.HOLD_DURATION_MINUTES)

        expired_bookings = db.query(model.Booking).filter(
            model.Booking.status == model.BookingStatus.PENDING,
            model.Booking.created_at <= expire_threshold
        ).all()

        for booking in expired_bookings:
            booking.status = model.BookingStatus.CANCELLED

            for seat in booking.assigned_units:
                seat.status = model.ItemStatus.AVAILABLE

            # Cancel Stripe PaymentIntent if one exists and is pending
            if booking.payment and booking.payment.stripe_payment_intent_id:
                if booking.payment.status == model.PaymentStatus.REQUIRES_PAYMENT_METHOD:
                    try:
                        stripe.PaymentIntent.cancel(booking.payment.stripe_payment_intent_id)
                        booking.payment.status = model.PaymentStatus.CANCELED
                    except Exception as e:
                        logger.warning(
                            f"Failed to cancel Stripe PaymentIntent {booking.payment.stripe_payment_intent_id}: {e}"
                        )

        if expired_bookings:
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