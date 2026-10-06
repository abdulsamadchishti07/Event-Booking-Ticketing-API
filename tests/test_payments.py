from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
import stripe
from sqlalchemy.orm import Session

from app.database import model


async def setup_event_and_booking(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """Helper fixture to create location, service, tier, seat, and customer booking."""
    now = datetime.now(timezone.utc)

    # 1. Location
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Karachi", "country": "Pakistan", "address_line": "Expo Center"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    # 2. Service
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Tech Conference 2026",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 50.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=4)).isoformat()
        }
    )
    assert service_res.status_code == 201
    service_id = service_res.json()["id"]

    # 3. Tier
    tier_res = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "VIP Pass", "price": 100.00}
    )
    assert tier_res.status_code == 201
    tier_id = tier_res.json()["id"]

    # 4. Inventory
    inv_res = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={"tier_id": tier_id, "identifier_codes": ["VIP-1", "VIP-2"]}
    )
    assert inv_res.status_code == 201

    seat = db_session.query(model.InventoryItems).filter_by(
        service_id=service_id, identifier_code="VIP-1"
    ).first()

    # 5. Customer creates hold
    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={
            "service_id": service_id,
            "tier_id": tier_id,
            "assigned_unit_ids": [seat.id],
            "quantity": 1
        }
    )
    assert book_res.status_code == 201
    booking_id = book_res.json()["id"]

    return service_id, tier_id, seat.id, booking_id


# ==========================================================
# 1. PAYMENT INTENT TESTS (POST /bookings/{id}/pay)
# ==========================================================

async def test_create_payment_intent_success(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    mock_intent = MagicMock()
    mock_intent.id = "pi_mock_123"
    mock_intent.client_secret = "pi_mock_123_secret_abc"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent) as mock_create:
        res = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

        assert res.status_code == 200
        data = res.json()
        assert data["stripe_payment_intent_id"] == "pi_mock_123"
        assert data["client_secret"] == "pi_mock_123_secret_abc"
        assert float(data["amount"]) == 100.00
        assert data["status"] == "requires_payment_method"

        # Verify Stripe API call arguments
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs["amount"] == 10000  # $100.00 in cents
        assert kwargs["currency"] == "usd"

    # Verify DB persistence
    payment = db_session.query(model.Payment).filter_by(booking_id=booking_id).first()
    assert payment is not None
    assert payment.stripe_payment_intent_id == "pi_mock_123"


async def test_create_payment_intent_idempotency(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    mock_intent = MagicMock()
    mock_intent.id = "pi_mock_idempotent"
    mock_intent.client_secret = "pi_secret_idempotent"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent) as mock_create:
        # First call creates the payment
        res1 = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
        assert res1.status_code == 200

        # Second call returns existing payment without creating another Stripe intent
        res2 = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
        assert res2.status_code == 200
        assert res2.json()["client_secret"] == "pi_secret_idempotent"

        assert mock_create.call_count == 1


async def test_create_payment_intent_unauthorized_user(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    # Seller attempts to pay for Customer's booking -> 403 Forbidden
    res = await client.post(f"/bookings/{booking_id}/pay", headers=seller_headers)
    assert res.status_code == 403
    assert "not authorized" in res.json()["detail"]


async def test_create_payment_intent_expired_hold(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    # Force hold to be expired (11 minutes ago)
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    booking.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    db_session.commit()

    res = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()


async def test_create_payment_intent_already_confirmed_or_cancelled(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()

    # Confirmed booking cannot be paid again
    booking.status = model.BookingStatus.CONFIRMED
    db_session.commit()
    res = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
    assert res.status_code == 400
    assert "already been paid" in res.json()["detail"]

    # Cancelled booking cannot be paid
    booking.status = model.BookingStatus.CANCELLED
    db_session.commit()
    res = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
    assert res.status_code == 400
    assert "cancelled" in res.json()["detail"]
