import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_
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
            try:
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

                        is_canceled = False
                        is_succeeded = False
                        try:
                            stripe.PaymentIntent.cancel(booking.payment.stripe_payment_intent_id)
                            is_canceled = True
                        except stripe.error.InvalidRequestError:
                            try:
                                pi = stripe.PaymentIntent.retrieve(booking.payment.stripe_payment_intent_id)
                                status_str = getattr(pi, "status", None)
                                if status_str == "canceled":
                                    is_canceled = True
                                elif status_str == "succeeded":
                                    is_succeeded = True
                            except Exception as retrieve_err:
                                logger.warning(f"Failed to retrieve Stripe intent: {retrieve_err}")
                                continue
                        except Exception as cancel_err:
                            logger.warning(f"Failed to cancel Stripe intent: {cancel_err}")
                            continue

                        if is_succeeded:
                            # Verify capacity before confirming to prevent overselling
                            can_confirm = False
                            if booking.service.booking_mode == model.BookingMode.UNIT_ASSIGNED:
                                unit_ids = sorted([u.id for u in booking.assigned_units])
                                locked_seats = (
                                    db.query(model.InventoryItems)
                                    .filter(model.InventoryItems.id.in_(unit_ids))
                                    .order_by(model.InventoryItems.id.asc())
                                    .with_for_update()
                                    .all()
                                )
                                can_confirm = len(locked_seats) == len(unit_ids) and all(
                                    unit.status == model.ItemStatus.RESERVED for unit in locked_seats
                                )
                            elif booking.service.booking_mode == model.BookingMode.SLOT_CAPACITY:
                                locked_svc = (
                                    db.query(model.Services)
                                    .filter(model.Services.id == booking.service_id)
                                    .with_for_update()
                                    .first()
                                )
                                now_dt = datetime.now(timezone.utc)
                                active_count = db.query(func.coalesce(func.sum(model.Booking.quantity), 0)).filter(
                                    model.Booking.service_id == booking.service_id,
                                    or_(
                                        model.Booking.status == model.BookingStatus.CONFIRMED,
                                        model.Booking.id == booking.id,
                                        and_(
                                            model.Booking.status == model.BookingStatus.PENDING,
                                            model.Booking.created_at >= now_dt - timedelta(minutes=settings.HOLD_DURATION_MINUTES)
                                        )
                                    )
                                ).scalar()
                                max_cap = locked_svc.max_capacity if locked_svc and locked_svc.max_capacity is not None else booking.service.max_capacity
                                can_confirm = (active_count <= max_cap)

                            if can_confirm:
                                from app.routers.payments import confirm_booking_and_invoice
                                confirm_booking_and_invoice(db, booking, booking.payment)
                                affected_service_ids.add(booking.service_id)
                                continue
                            else:
                                # Over capacity / hold lost: auto-refund and cancel
                                try:
                                    stripe.Refund.create(
                                        payment_intent=booking.payment.stripe_payment_intent_id,
                                        amount=int(booking.payment.amount * 100),
                                        reason="requested_by_customer",
                                        idempotency_key=f"sweeper_refund_{booking.payment.stripe_payment_intent_id}"
                                    )
                                except Exception as refund_err:
                                    logger.error(f"Sweeper auto-refund failed for {booking.id}: {refund_err}")
                                booking.payment.status = model.PaymentStatus.CANCELED

                        elif is_canceled:
                            booking.payment.status = model.PaymentStatus.CANCELED
                        else:
                            # Not canceled and not succeeded: skip booking
                            continue

                booking.status = model.BookingStatus.CANCELLED
                for seat in booking.assigned_units:
                    seat.status = model.ItemStatus.AVAILABLE

                affected_service_ids.add(booking.service_id)
                db.commit()

            except Exception as item_err:
                logger.error(f"Error processing expired booking {b_id}: {item_err}", exc_info=True)
                db.rollback()

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