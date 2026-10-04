import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db, model, schema
from app.core import oauth2
from app.core.config import settings

# Initialize Stripe API key
stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(
    prefix="/bookings",
    tags=["Payments"]
)

HOLD_DURATION_MINUTES = 10


@router.post(
    "/{booking_id}/pay",
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
