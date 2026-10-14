import logging
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, status, Request, Header, BackgroundTasks
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_

from app.database import get_db, model, schema
from app.core import oauth2, redis as redis_client
from app.core.cache import invalidate_services_cache
from app.core.config import settings
from app.services import email

logger = logging.getLogger(__name__)

# Initialize Stripe API key
stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(
    tags=["Payments"]
)

HOLD_DURATION_MINUTES = settings.HOLD_DURATION_MINUTES


def confirm_booking_and_invoice(
    db: Session,
    booking: model.Booking | None,
    payment: model.Payment,
    background_tasks: BackgroundTasks | None = None
) -> tuple[model.Payment, model.Invoice]:
    """
    Atomic transition of payment to SUCCEEDED and booking to CONFIRMED.
    Transitions assigned units to BOOKED, generates an immutable Invoice,
    commits transaction, and dispatches invoice email in background.
    """
    payment.status = model.PaymentStatus.SUCCEEDED

    if booking:
        booking.status = model.BookingStatus.CONFIRMED
        for unit in booking.assigned_units:
            unit.status = model.ItemStatus.BOOKED

    existing_invoice = db.query(model.Invoice).filter(
        model.Invoice.payment_id == payment.id
    ).first()

    if not existing_invoice:
        invoice_number = f"INV-{datetime.now(timezone.utc).year}-{payment.id:06d}"
        invoice = model.Invoice(
            payment_id=payment.id,
            invoice_number=invoice_number
        )
        db.add(invoice)
    else:
        invoice = existing_invoice
        invoice_number = existing_invoice.invoice_number

    db.commit()
    db.refresh(payment)

    if background_tasks and booking:
        background_tasks.add_task(invalidate_services_cache, service_id=booking.service_id)
        if booking.user and booking.user.email:
            background_tasks.add_task(
                email.send_invoice_email,
                to_email=booking.user.email,
                user_name=booking.user.name,
                invoice_number=invoice_number,
                event_name=booking.service.service_name if booking.service else "Event Ticket",
                quantity=booking.quantity,
                total_amount=str(payment.amount),
                booking_id=booking.id
            )

    return payment, invoice


@router.post(
    "/bookings/{booking_id}/pay",
    response_model=schema.PaymentOut,
    status_code=status.HTTP_200_OK,
    summary="Create or retrieve Stripe PaymentIntent for a pending booking",
    dependencies=[
        Depends(
            redis_client.IPRateLimiting(
                max_requests=60,
                window_seconds=60
            )
        )
    ]
)
def create_payment_intent_for_booking(
    booking_id: int,
    background_tasks: BackgroundTasks,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    """
    Initiates payment for an active reservation hold:
    1. Validates booking ownership with pessimistic lock.
    2. Enforces active hold TTL.
    3. Calculates total amount on the server (zero-trust, prioritizing unit tiers).
    4. Handles free events ($0.00) without external gateway.
    5. Creates Stripe PaymentIntent with a deterministic idempotency key.
    6. Saves payment record and returns client_secret.
    """
    now = datetime.now(timezone.utc)

    # 1. Fetch booking with pessimistic row lock
    booking = db.query(model.Booking).filter(model.Booking.id == booking_id).with_for_update().first()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking with id {booking_id} not found"
        )

    # 2. Ownership verification
    if booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to pay for this booking"
        )

    # 3. Status validation
    if booking.status == model.BookingStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This booking has already been paid and confirmed"
        )
    if booking.status == model.BookingStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This booking has been cancelled and cannot be paid for"
        )

    # 4. Enforce hold TTL
    booking_created = booking.created_at
    if booking_created.tzinfo is None:
        booking_created = booking_created.replace(tzinfo=timezone.utc)
    hold_expiry = booking_created + timedelta(minutes=HOLD_DURATION_MINUTES)
    if now > hold_expiry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reservation hold has expired. Please create a new booking."
        )

    # 5. Check if a Payment already exists for this booking (Reconcile / Idempotency)
    existing_payment = db.query(model.Payment).filter(model.Payment.booking_id == booking_id).first()
    if existing_payment:
        if existing_payment.status == model.PaymentStatus.SUCCEEDED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This booking has already been paid and confirmed"
            )
        # Reusable active pending payment: verify it has not been cancelled or succeeded on Stripe
        if existing_payment.status in [
            model.PaymentStatus.REQUIRES_PAYMENT_METHOD,
            model.PaymentStatus.REQUIRES_CONFIRMATION,
            model.PaymentStatus.PROCESSING
        ]:
            if not existing_payment.stripe_payment_intent_id.startswith("free_"):
                try:
                    intent = stripe.PaymentIntent.retrieve(existing_payment.stripe_payment_intent_id)
                    if intent.status == "succeeded":
                        # Payment already completed on Stripe! Reconcile immediately with invoice & email
                        confirm_booking_and_invoice(db, booking, existing_payment, background_tasks)
                        return existing_payment
                    elif intent.status in ["requires_payment_method", "requires_confirmation", "requires_action", "processing"]:
                        return existing_payment
                    elif intent.status == "canceled":
                        existing_payment.status = model.PaymentStatus.CANCELED
                        db.commit()
                except Exception as e:
                    logger.warning(f"Could not retrieve Stripe intent {existing_payment.stripe_payment_intent_id}: {e}")
                    return existing_payment
            else:
                return existing_payment

        # If payment had failed (e.g. card declined), reconcile with Stripe
        if existing_payment.status == model.PaymentStatus.FAILED:
            try:
                intent = stripe.PaymentIntent.retrieve(existing_payment.stripe_payment_intent_id)
                if intent.status == "succeeded":
                    confirm_booking_and_invoice(db, booking, existing_payment, background_tasks)
                    return existing_payment
                # If still open on Stripe, reactivate and return client_secret
                elif intent.status in ["requires_payment_method", "requires_confirmation", "requires_action"]:
                    existing_payment.status = model.PaymentStatus.REQUIRES_PAYMENT_METHOD
                    existing_payment.client_secret = intent.client_secret
                    db.commit()
                    db.refresh(existing_payment)
                    return existing_payment
                elif intent.status == "canceled":
                    existing_payment.status = model.PaymentStatus.CANCELED
                    db.commit()
            except Exception as e:
                logger.warning(f"Could not retrieve Stripe intent {existing_payment.stripe_payment_intent_id}: {e}")

    # 6. Calculate total amount (Zero-Trust Server Calculation)
    # Unit-assigned bookings MUST price from the seats' own tiers to prevent tier manipulation
    total_amount: Decimal = Decimal("0.00")
    if booking.assigned_units:
        total_amount = sum(
            Decimal(unit.tier.price) if unit.tier else Decimal(booking.service.base_price)
            for unit in booking.assigned_units
        )
    elif booking.tier:
        total_amount = Decimal(booking.tier.price) * booking.quantity
    else:
        total_amount = Decimal(booking.service.base_price) * booking.quantity

    if not existing_payment:
        idempotency_key = f"booking_{booking.id}"
    else:
        # Create a new, uniquely identified retry attempt to avoid reusing a cancelled intent on Stripe
        retry_token = uuid.uuid4().hex[:8]
        idempotency_key = f"booking_{booking.id}_attempt_{retry_token}"

    # 7. Free Event Handling: auto-confirm without calling Stripe
    if total_amount <= Decimal("0.00"):
        total_amount = Decimal("0.00")
        if existing_payment:
            existing_payment.amount = Decimal("0.00")
            payment = existing_payment
        else:
            payment = model.Payment(
                booking_id=booking.id,
                stripe_payment_intent_id=f"free_booking_{booking.id}",
                client_secret="free_booking",
                idempotency_key=idempotency_key,
                amount=total_amount,
                currency="usd",
                status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD
            )
            db.add(payment)
            db.flush()

        confirm_booking_and_invoice(db, booking, payment, background_tasks)
        return payment

    # Convert to integer cents for Stripe (e.g. 20.00 -> 2000)
    amount_in_cents = int(total_amount * 100)

    # 8. Call Stripe API to create PaymentIntent
    try:
        payment_intent = stripe.PaymentIntent.create(
            amount=amount_in_cents,
            currency="usd",
            metadata={
                "booking_id": str(booking.id),
                "user_id": str(current_user.id),
                "service_id": str(booking.service_id)
            },
            idempotency_key=idempotency_key
        )
    except stripe.StripeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Payment Gateway Error: {str(e.user_message or e)}"
        )

    # 9. Store or update payment record in database
    if existing_payment:
        existing_payment.stripe_payment_intent_id = payment_intent.id
        existing_payment.client_secret = payment_intent.client_secret
        existing_payment.idempotency_key = idempotency_key
        existing_payment.amount = total_amount
        existing_payment.status = model.PaymentStatus.REQUIRES_PAYMENT_METHOD
        db.commit()
        db.refresh(existing_payment)
        return existing_payment

    payment = model.Payment(
        booking_id=booking.id,
        stripe_payment_intent_id=payment_intent.id,
        client_secret=payment_intent.client_secret,
        idempotency_key=idempotency_key,
        amount=total_amount,
        currency="usd",
        status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD
    )

    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


@router.post(
    "/webhook/stripe",
    status_code=status.HTTP_200_OK,
    summary="Cryptographically verified Stripe Webhook handler"
)
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    stripe_signature: Annotated[str | None, Header(alias="stripe-signature")] = None,
    db: Session = Depends(get_db)
):
    """
    Listens for asynchronous webhook events from Stripe:
    1. Reads raw payload bytes asynchronously.
    2. Validates HMAC-SHA256 signature using STRIPE_WEBHOOK_SECRET.
    3. Handles payment_intent.succeeded (auto-refunds if booking cancelled; else confirms booking, books seats, generates invoice).
    4. Handles payment_intent.payment_failed (records failure without cancelling booking to allow retries).
    """
    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing stripe-signature header"
        )

    payload = await request.body()

    # 1. Cryptographic Signature Verification
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payload"
        )
    except stripe.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cryptographic signature"
        )

    event_type = event.get("type")
    data_obj = event.get("data", {}).get("object", {})
    payment_intent_id = data_obj.get("id")

    # 2. Locate Payment record
    payment = await run_in_threadpool(
        lambda: db.query(model.Payment).filter(model.Payment.stripe_payment_intent_id == payment_intent_id).first()
    )
    if not payment:
        return {"status": "untracked_intent", "event": event_type}

    # Lock booking row with PostgreSQL pessimistic lock to coordinate with sweeper
    booking = await run_in_threadpool(
        lambda: (
            db.query(model.Booking)
            .filter(model.Booking.id == payment.booking_id)
            .with_for_update()
            .first()
            if payment.booking_id
            else None
        )
    )

    # 3. Handle payment_intent.succeeded
    if event_type == "payment_intent.succeeded":
        # Idempotency guard: ignore duplicate deliveries
        if payment.status == model.PaymentStatus.SUCCEEDED:
            return {"status": "already_processed"}
        if payment.status == model.PaymentStatus.CANCELED:
            existing_cancellation = await run_in_threadpool(
                lambda: db.query(model.Cancellation).filter(
                    model.Cancellation.booking_id == payment.booking_id
                ).first()
            )
            if existing_cancellation and existing_cancellation.refund_amount > Decimal("0.00"):
                return {"status": "already_refunded"}

        # Check inventory claim validity under pessimistic locks
        can_confirm = False
        if booking and booking.status == model.BookingStatus.PENDING:
            if booking.service.booking_mode == model.BookingMode.UNIT_ASSIGNED:
                def check_and_lock_unit_assigned():
                    if not booking.assigned_units:
                        return False
                    unit_ids = sorted([u.id for u in booking.assigned_units])
                    locked_seats = (
                        db.query(model.InventoryItems)
                        .filter(model.InventoryItems.id.in_(unit_ids))
                        .order_by(model.InventoryItems.id.asc())
                        .with_for_update()
                        .all()
                    )
                    return len(locked_seats) == len(unit_ids) and all(
                        unit.status == model.ItemStatus.RESERVED for unit in locked_seats
                    )

                can_confirm = await run_in_threadpool(check_and_lock_unit_assigned)

            elif booking.service.booking_mode == model.BookingMode.SLOT_CAPACITY:
                def check_and_lock_slot_capacity():
                    # Synchronize with create_booking by locking the service row with PostgreSQL pessimistic lock
                    locked_svc = (
                        db.query(model.Services)
                        .filter(model.Services.id == booking.service_id)
                        .with_for_update()
                        .first()
                    )
                    now = datetime.now(timezone.utc)
                    active_booking_count = db.query(func.coalesce(func.sum(model.Booking.quantity), 0)).filter(
                        model.Booking.service_id == booking.service_id,
                        or_(
                            model.Booking.status == model.BookingStatus.CONFIRMED,
                            model.Booking.id == booking.id,
                            and_(
                                model.Booking.status == model.BookingStatus.PENDING,
                                model.Booking.created_at >= now - timedelta(minutes=HOLD_DURATION_MINUTES)
                            )
                        )
                    ).scalar()
                    max_cap = locked_svc.max_capacity if locked_svc and locked_svc.max_capacity is not None else booking.service.max_capacity
                    return active_booking_count <= max_cap

                can_confirm = await run_in_threadpool(check_and_lock_slot_capacity)

        # If booking was cancelled, or inventory was released and taken: DO NOT confirm! Initiate auto-refund.
        if not can_confirm:
            # Execute refund FIRST with deterministic idempotency key BEFORE committing CANCELED state
            try:
                await run_in_threadpool(
                    stripe.Refund.create,
                    payment_intent=payment_intent_id,
                    amount=int(payment.amount * 100),
                    reason="requested_by_customer",
                    idempotency_key=f"auto_refund_{payment_intent_id}"
                )
            except Exception as e:
                logger.error(f"Auto-refund failed for expired/cancelled booking {booking.id if booking else 'unknown'}: {e}")
                await run_in_threadpool(db.rollback)
                # Raise 500 error so Stripe automatically retries the webhook delivery until refund succeeds!
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Auto-refund processing failed: {str(e)}. Webhook will be retried."
                )

            # Refund confirmed by Stripe: update database records
            payment.status = model.PaymentStatus.CANCELED
            if booking:
                was_pending = (booking.status == model.BookingStatus.PENDING)
                booking.status = model.BookingStatus.CANCELLED
                # Only free seats if this booking was still pending and held them; do not touch seats held by others
                if was_pending:
                    for unit in booking.assigned_units:
                        if unit.status == model.ItemStatus.RESERVED:
                            unit.status = model.ItemStatus.AVAILABLE

                # Record cancellation audit log if not already recorded
                existing_cancellation = await run_in_threadpool(
                    lambda: db.query(model.Cancellation).filter(
                        model.Cancellation.booking_id == booking.id
                    ).first()
                )
                if not existing_cancellation:
                    cancellation = model.Cancellation(
                        booking_id=booking.id,
                        refund_amount=payment.amount,
                        reason="Auto-refunded: payment arrived after hold expired or booking was cancelled"
                    )
                    db.add(cancellation)

            await run_in_threadpool(db.commit)
            if booking:
                background_tasks.add_task(invalidate_services_cache, service_id=booking.service_id)
            return {"status": "auto_refunded_cancelled_booking", "event": event_type}

        await run_in_threadpool(confirm_booking_and_invoice, db, booking, payment, background_tasks)
        return {"status": "success", "event": event_type}

    # 4. Handle payment_intent.payment_failed
    elif event_type == "payment_intent.payment_failed":
        # Ignore stale failure events if payment has reached terminal status
        if payment.status in (model.PaymentStatus.SUCCEEDED, model.PaymentStatus.CANCELED):
            logger.info(
                f"Ignoring payment_failed event for payment {payment.id} because it is in terminal status: {payment.status}"
            )
            return {"status": "ignored_terminal_state", "event": event_type}

        payment.status = model.PaymentStatus.FAILED
        # Do NOT cancel booking on first card decline; let the hold expire or customer retry
        await run_in_threadpool(db.commit)
        return {"status": "failed_recorded", "event": event_type}

    # Unhandled event types acknowledged with 200 OK so Stripe stops retrying
    return {"status": "ignored", "event": event_type}


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=schema.CancellationOut,
    status_code=status.HTTP_200_OK,
    summary="Cancel a booking, issue Stripe refund if paid, and release seats"
)
def cancel_booking_and_refund(
    booking_id: int,
    background_tasks: BackgroundTasks,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    cancellation_in: schema.CancellationBase = None,
    db: Session = Depends(get_db)
):
    """
    Cancels a booking and reverses financial settlement:
    1. Verifies ownership and cancellation policy window with pessimistic row lock.
    2. If confirmed, issues a full refund via Stripe Refunds API with deterministic idempotency key.
    3. If pending, cancels any active Stripe PaymentIntent.
    4. Reverts seats back to 'available'.
    5. Records an audit row in cancellations table.
    """
    now = datetime.now(timezone.utc)

    # 1. Fetch booking with pessimistic lock
    booking = db.query(model.Booking).filter(model.Booking.id == booking_id).with_for_update().first()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking with id {booking_id} not found"
        )

    # 2. Ownership verification (Customer or Admin)
    if booking.user_id != current_user.id and current_user.role != model.UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to cancel this booking"
        )

    # 3. Check if already cancelled
    if booking.status == model.BookingStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This booking has already been cancelled"
        )

    # 4. Check if event has already started
    if booking.service and booking.service.start_time:
        service_start = booking.service.start_time
        if service_start.tzinfo is None:
            service_start = service_start.replace(tzinfo=timezone.utc)
        if now >= service_start:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot cancel tickets for an event that has already started"
            )

    refund_amount = Decimal("0.00")

    # 5. Process Stripe Refund if booking was confirmed/paid
    if booking.status == model.BookingStatus.CONFIRMED:
        payment = booking.payment
        if payment and payment.status == model.PaymentStatus.SUCCEEDED:
            is_free_intent = (
                payment.stripe_payment_intent_id.startswith("free_")
                if payment.stripe_payment_intent_id
                else False
            )
            if payment.amount > Decimal("0.00") and not is_free_intent:
                if not payment.stripe_payment_intent_id:
                    logger.error(
                        f"Inconsistent payment record: payment {payment.id} is SUCCEEDED with amount {payment.amount} but has null stripe_payment_intent_id"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Inconsistent payment record: missing Stripe payment intent ID for paid booking"
                    )

                amount_in_cents = int(payment.amount * 100)
                try:
                    stripe.Refund.create(
                        payment_intent=payment.stripe_payment_intent_id,
                        amount=amount_in_cents,
                        reason="requested_by_customer",
                        idempotency_key=f"refund_booking_{booking.id}"
                    )
                except stripe.StripeError as e:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Stripe Refund Gateway Error: {str(e.user_message or e)}"
                    )

            payment.status = model.PaymentStatus.CANCELED
            refund_amount = payment.amount

    elif booking.payment and booking.payment.stripe_payment_intent_id:
        is_free_intent = booking.payment.stripe_payment_intent_id.startswith("free_")
        # Cancel pending/failed/processing Stripe PaymentIntent if booking was not confirmed
        if (
            booking.payment.status not in (model.PaymentStatus.CANCELED, model.PaymentStatus.SUCCEEDED)
            and not is_free_intent
        ):
            try:
                stripe.PaymentIntent.cancel(booking.payment.stripe_payment_intent_id)
                booking.payment.status = model.PaymentStatus.CANCELED
            except Exception as e:
                logger.warning(f"Could not cancel Stripe PaymentIntent: {e}")

    # 6. Revert booking and seat status
    booking.status = model.BookingStatus.CANCELLED

    for unit in booking.assigned_units:
        unit.status = model.ItemStatus.AVAILABLE

    # 7. Record immutable cancellation audit log
    reason_text = cancellation_in.reason if cancellation_in and cancellation_in.reason else "Cancelled by customer"
    cancellation = model.Cancellation(
        booking_id=booking.id,
        refund_amount=refund_amount,
        reason=reason_text
    )
    db.add(cancellation)
    db.commit()
    db.refresh(cancellation)

    # Send cancellation email in background
    if booking:
        background_tasks.add_task(invalidate_services_cache, service_id=booking.service_id)
        if booking.user and booking.user.email:
            background_tasks.add_task(
                email.send_cancellation_email,
                to_email=booking.user.email,
                user_name=booking.user.name,
                event_name=booking.service.service_name if booking.service else "Event Ticket",
                refund_amount=str(refund_amount),
                reason=reason_text,
                booking_id=booking.id
            )

    return cancellation
