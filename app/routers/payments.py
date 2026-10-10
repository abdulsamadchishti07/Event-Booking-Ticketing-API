import logging
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, status, Request, Header, BackgroundTasks
from sqlalchemy.orm import Session

from app.database import get_db, model, schema
from app.core import oauth2
from app.core.config import settings
from app.services import email

logger = logging.getLogger(__name__)

# Initialize Stripe API key
stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(
    tags=["Payments"]
)

HOLD_DURATION_MINUTES = settings.HOLD_DURATION_MINUTES


@router.post(
    "/bookings/{booking_id}/pay",
    response_model=schema.PaymentOut,
    status_code=status.HTTP_200_OK,
    summary="Create or retrieve Stripe PaymentIntent for a pending booking"
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
    hold_expiry = booking.created_at + timedelta(minutes=HOLD_DURATION_MINUTES)
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
        # Reusable active pending payment: verify it has not been cancelled on Stripe
        if existing_payment.status in [
            model.PaymentStatus.REQUIRES_PAYMENT_METHOD,
            model.PaymentStatus.REQUIRES_CONFIRMATION,
            model.PaymentStatus.PROCESSING
        ]:
            if not existing_payment.stripe_payment_intent_id.startswith("free_"):
                try:
                    intent = stripe.PaymentIntent.retrieve(existing_payment.stripe_payment_intent_id)
                    if intent.status in ["requires_payment_method", "requires_confirmation", "requires_action", "processing"]:
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
                # If still open on Stripe, reactivate and return client_secret
                if intent.status in ["requires_payment_method", "requires_confirmation", "requires_action"]:
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
        booking.status = model.BookingStatus.CONFIRMED
        for unit in booking.assigned_units:
            unit.status = model.ItemStatus.BOOKED

        if existing_payment:
            existing_payment.amount = Decimal("0.00")
            existing_payment.status = model.PaymentStatus.SUCCEEDED
            payment = existing_payment
        else:
            payment = model.Payment(
                booking_id=booking.id,
                stripe_payment_intent_id=f"free_booking_{booking.id}",
                client_secret="free_booking",
                idempotency_key=idempotency_key,
                amount=total_amount,
                currency="usd",
                status=model.PaymentStatus.SUCCEEDED
            )
            db.add(payment)
        db.flush()

        invoice_number = f"INV-{datetime.now(timezone.utc).year}-{payment.id:06d}"
        invoice = model.Invoice(
            payment_id=payment.id,
            invoice_number=invoice_number
        )
        db.add(invoice)
        db.commit()
        db.refresh(payment)

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
    payment = db.query(model.Payment).filter(model.Payment.stripe_payment_intent_id == payment_intent_id).first()
    if not payment:
        return {"status": "untracked_intent", "event": event_type}

    # Lock booking row with PostgreSQL pessimistic lock to coordinate with sweeper
    booking = (
        db.query(model.Booking)
        .filter(model.Booking.id == payment.booking_id)
        .with_for_update()
        .first()
        if payment.booking_id
        else None
    )

    # 3. Handle payment_intent.succeeded
    if event_type == "payment_intent.succeeded":
        # Idempotency guard: ignore duplicate deliveries
        if payment.status == model.PaymentStatus.SUCCEEDED:
            return {"status": "already_processed"}

        now = datetime.now(timezone.utc)
        hold_expiry = (booking.created_at + timedelta(minutes=HOLD_DURATION_MINUTES)) if booking else None
        is_hold_expired = (now > hold_expiry) if hold_expiry else True

        # Before confirming, independently verify booking is still PENDING and hold has not expired!
        if not booking or booking.status != model.BookingStatus.PENDING or is_hold_expired:
            # Hold has expired or booking was cancelled: DO NOT confirm!
            if booking:
                booking.status = model.BookingStatus.CANCELLED
                # Free reserved seats back to available immediately
                for unit in booking.assigned_units:
                    unit.status = model.ItemStatus.AVAILABLE

            payment.status = model.PaymentStatus.CANCELED
            db.commit()

            try:
                stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    amount=int(payment.amount * 100),
                    reason="requested_by_customer",
                    idempotency_key=f"auto_refund_{payment_intent_id}"
                )
            except Exception as e:
                logger.error(f"Auto-refund failed for expired/cancelled booking {booking.id if booking else 'unknown'}: {e}")
                # Raise 500 error so Stripe automatically retries the webhook delivery until refund succeeds!
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Auto-refund processing failed: {str(e)}. Webhook will be retried."
                )

            return {"status": "auto_refunded_cancelled_booking", "event": event_type}

        payment.status = model.PaymentStatus.SUCCEEDED

        if booking:
            booking.status = model.BookingStatus.CONFIRMED

            # Transition seats from 'reserved' to 'booked'
            for unit in booking.assigned_units:
                unit.status = model.ItemStatus.BOOKED

        # Auto-generate immutable Invoice
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
            invoice_number = existing_invoice.invoice_number
        db.commit()

        # Send invoice email in background
        if booking and booking.user and booking.user.email:
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
        return {"status": "success", "event": event_type}

    # 4. Handle payment_intent.payment_failed
    elif event_type == "payment_intent.payment_failed":
        payment.status = model.PaymentStatus.FAILED
        # Do NOT cancel booking on first card decline; let the hold expire or customer retry
        db.commit()
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
    if booking.service and now >= booking.service.start_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel tickets for an event that has already started"
        )

    refund_amount = Decimal("0.00")

    # 5. Process Stripe Refund if booking was confirmed/paid
    if booking.status == model.BookingStatus.CONFIRMED:
        payment = booking.payment
        if payment and payment.status == model.PaymentStatus.SUCCEEDED:
            if payment.amount > Decimal("0.00") and not payment.stripe_payment_intent_id.startswith("free_"):
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

    elif booking.payment and booking.payment.stripe_payment_intent_id and not booking.payment.stripe_payment_intent_id.startswith("free_"):
        # Cancel pending Stripe PaymentIntent if booking was not confirmed
        if booking.payment.status == model.PaymentStatus.REQUIRES_PAYMENT_METHOD:
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
    if booking and booking.user and booking.user.email:
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
