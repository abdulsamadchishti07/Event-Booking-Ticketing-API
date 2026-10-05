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
from app.services.email import send_invoice_email, send_cancellation_email

# Initialize Stripe API key
stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(
    tags=["Payments"]
)

HOLD_DURATION_MINUTES = 10


@router.post(
    "/bookings/{booking_id}/pay",
    response_model=schema.PaymentOut,
    status_code=status.HTTP_200_OK,
    summary="Create or retrieve Stripe PaymentIntent for a pending booking"
)
def create_payment_intent_for_booking(
    booking_id: int,
    current_user: Annotated[model.User, Depends(oauth2.get_current_user)],
    db: Session = Depends(get_db)
):
    """
    Initiates payment for an active reservation hold:
    1. Validates booking ownership.
    2. Enforces active 10-minute hold TTL.
    3. Calculates total amount on the server (zero-trust).
    4. Creates Stripe PaymentIntent with an idempotency key.
    5. Saves payment record and returns client_secret.
    """
    now = datetime.now(timezone.utc)

    # 1. Fetch booking
    booking = db.query(model.Booking).filter(model.Booking.id == booking_id).first()
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

    # 4. Enforce 10-minute hold TTL
    hold_expiry = booking.created_at + timedelta(minutes=HOLD_DURATION_MINUTES)
    if now > hold_expiry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reservation hold has expired. Please create a new booking."
        )

    # 5. Check if a Payment already exists for this booking (Idempotency)
    existing_payment = db.query(model.Payment).filter(model.Payment.booking_id == booking_id).first()
    if existing_payment:
        return existing_payment

    # 6. Calculate total amount (Zero-Trust Server Calculation)
    total_amount: Decimal = Decimal("0.00")
    if booking.tier:
        total_amount = Decimal(booking.tier.price) * booking.quantity
    elif booking.assigned_units:
        total_amount = sum(Decimal(unit.tier.price) for unit in booking.assigned_units if unit.tier)
    else:
        total_amount = Decimal(booking.service.base_price) * booking.quantity

    if total_amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Total amount must be greater than zero"
        )

    # Convert to integer cents for Stripe (e.g. 20.00 -> 2000)
    amount_in_cents = int(total_amount * 100)
    idempotency_key = f"booking_{booking.id}_{uuid.uuid4()}"

    # 7. Call Stripe API to create PaymentIntent
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

    # 8. Store payment record in database
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
    stripe_signature: Annotated[str|None, Header(alias="stripe-signature")] = None,
    db: Session= Depends(get_db)
):
    """
    Listens for asynchronous webhook events from Stripe:
    1. Reads raw payload bytes.
    2. Validates HMAC-SHA256 signature using STRIPE_WEBHOOK_SECRET.
    3. Handles payment_intent.succeeded (confirms booking, books seats, generates invoice).
    4. Handles payment_intent.payment_failed (cancels booking, frees seats).
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
    payment_intent_id =  data_obj.get("id")

    # 2. Locate Payment record
    payment = db.query(model.Payment).filter(model.Payment.stripe_payment_intent_id == payment_intent_id).first()
    if not payment:
        # Event for an intent not tracked in this database (or test event); return 200 to acknowledge
        return {"status": "untracked_intent", "event": event_type}

    booking = payment.booking

    # 3. Handle payment_intent.succeeded
    if event_type == "payment_intent.succeeded":
        # Idempotency guard: ignore duplicate deliveries
        if payment.status == model.PaymentStatus.SUCCEEDED:
            return {"status": "already_processed"}
        
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
                send_invoice_email,
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
    
        if booking and booking.status != model.BookingStatus.CONFIRMED:
            booking.status = model.BookingStatus.CANCELLED

            # Release reserved seats back to 'available'
            for unit in booking.assigned_units:
                unit.status = model.ItemStatus.AVAILABLE

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
    current_user: Annotated[model.User, Depends(oauth2.get_current_user)],
    cancellation_in: schema.CancellationBase = None,
    db: Session = Depends(get_db)
):
    """
    Cancels a booking and reverses financial settlement:
    1. Verifies ownership and cancellation policy window.
    2. If confirmed, issues a full refund via Stripe Refunds API.
    3. Reverts seats back to 'available'.
    4. Records an audit row in cancellations table.
    """
    now = datetime.now(timezone.utc)

    # 1. Fetch booking
    booking = db.query(model.Booking).filter(model.Booking.id == booking_id).first()
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
            amount_in_cents = int(payment.amount * 100)
            try:
                stripe.Refund.create(
                    payment_intent=payment.stripe_payment_intent_id,
                    amount=amount_in_cents,
                    reason="requested_by_customer"
                )
            except stripe.StripeError as e:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Stripe Refund Gateway Error: {str(e.user_message or e)}"
                )

            payment.status = model.PaymentStatus.CANCELED
            refund_amount = payment.amount

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
            send_cancellation_email,
            to_email=booking.user.email,
            user_name=booking.user.name,
            event_name=booking.service.service_name if booking.service else "Event Ticket",
            refund_amount=str(refund_amount),
            reason=reason_text,
            booking_id=booking.id
        )
        
    return cancellation



