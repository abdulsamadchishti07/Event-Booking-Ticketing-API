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


# ==========================================================
# 2. STRIPE WEBHOOK TESTS (POST /webhook/stripe)
# ==========================================================

async def test_webhook_missing_signature(client: httpx.AsyncClient):
    res = await client.post("/webhook/stripe", content=b"{}")
    assert res.status_code == 400
    assert "Missing stripe-signature header" in res.json()["detail"]


async def test_webhook_invalid_signature(client: httpx.AsyncClient):
    with patch("stripe.Webhook.construct_event", side_effect=stripe.SignatureVerificationError("Invalid sig", "sig")):
        res = await client.post(
            "/webhook/stripe",
            content=b'{"id": "evt_test"}',
            headers={"stripe-signature": "tampered_signature"}
        )
        assert res.status_code == 400
        assert "Invalid cryptographic signature" in res.json()["detail"]


async def test_webhook_payment_succeeded(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, seat_id, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    # 1. Create payment intent
    mock_intent = MagicMock()
    mock_intent.id = "pi_webhook_success_123"
    mock_intent.client_secret = "secret_success"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        pay_res = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
        assert pay_res.status_code == 200

    # 2. Simulate Stripe sending payment_intent.succeeded webhook
    mock_event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_webhook_success_123",
                "amount": 10000,
                "currency": "usd"
            }
        }
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        webhook_res = await client.post(
            "/webhook/stripe",
            content=b'{"type": "payment_intent.succeeded"}',
            headers={"stripe-signature": "valid_mock_signature"}
        )
        assert webhook_res.status_code == 200
        assert webhook_res.json()["status"] == "success"

    # 3. Assert State Transitions
    db_session.expire_all()
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    payment = db_session.query(model.Payment).filter_by(booking_id=booking_id).first()
    seat = db_session.query(model.InventoryItems).filter_by(id=seat_id).first()
    invoice = db_session.query(model.Invoice).filter_by(payment_id=payment.id).first()

    assert booking.status == model.BookingStatus.CONFIRMED
    assert payment.status == model.PaymentStatus.SUCCEEDED
    assert seat.status == model.ItemStatus.BOOKED
    assert invoice is not None
    assert invoice.invoice_number.startswith("INV-")


async def test_webhook_idempotency_duplicate_delivery(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    mock_intent = MagicMock()
    mock_intent.id = "pi_duplicate_test"
    mock_intent.client_secret = "secret_dup"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

    mock_event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_duplicate_test"}}
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        # First webhook delivery
        res1 = await client.post("/webhook/stripe", content=b"{}", headers={"stripe-signature": "sig"})
        assert res1.status_code == 200
        assert res1.json()["status"] == "success"

        # Duplicate webhook delivery
        res2 = await client.post("/webhook/stripe", content=b"{}", headers={"stripe-signature": "sig"})
        assert res2.status_code == 200
        assert res2.json()["status"] == "already_processed"

    # Assert exactly 1 invoice exists
    payment = db_session.query(model.Payment).filter_by(stripe_payment_intent_id="pi_duplicate_test").first()
    invoice_count = db_session.query(model.Invoice).filter_by(payment_id=payment.id).count()
    assert invoice_count == 1


async def test_webhook_payment_failed(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, seat_id, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    mock_intent = MagicMock()
    mock_intent.id = "pi_fail_test"
    mock_intent.client_secret = "secret_fail"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

    mock_event = {
        "type": "payment_intent.payment_failed",
        "data": {"object": {"id": "pi_fail_test"}}
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        webhook_res = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert webhook_res.status_code == 200
        assert webhook_res.json()["status"] == "failed_recorded"

    db_session.expire_all()
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    payment = db_session.query(model.Payment).filter_by(booking_id=booking_id).first()
    seat = db_session.query(model.InventoryItems).filter_by(id=seat_id).first()

    assert booking.status == model.BookingStatus.CANCELLED
    assert payment.status == model.PaymentStatus.FAILED
    assert seat.status == model.ItemStatus.AVAILABLE

# ==========================================================
# 3. CANCELLATION & REFUND TESTS (POST /bookings/{id}/cancel)
# ==========================================================

async def test_cancel_booking_and_refund_confirmed(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, seat_id, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    # Set up confirmed booking with successful payment
    payment = model.Payment(
        booking_id=booking_id,
        stripe_payment_intent_id="pi_to_refund_123",
        client_secret="secret_refund",
        idempotency_key="key_refund",
        amount=Decimal("100.00"),
        currency="usd",
        status=model.PaymentStatus.SUCCEEDED
    )
    db_session.add(payment)

    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    booking.status = model.BookingStatus.CONFIRMED

    seat = db_session.query(model.InventoryItems).filter_by(id=seat_id).first()
    seat.status = model.ItemStatus.BOOKED
    db_session.commit()

    with patch("stripe.Refund.create") as mock_refund:
        mock_refund.return_value = MagicMock(id="re_mock_123", status="succeeded")

        cancel_res = await client.post(
            f"/bookings/{booking_id}/cancel",
            headers=user_headers,
            json={"reason": "Cannot attend due to travel"}
        )

        assert cancel_res.status_code == 200
        data = cancel_res.json()
        assert float(data["refund_amount"]) == 100.00
        assert data["reason"] == "Cannot attend due to travel"

        # Verify Stripe Refund API was called with amount in cents
        mock_refund.assert_called_once_with(
            payment_intent="pi_to_refund_123",
            amount=10000,
            reason="requested_by_customer"
        )

    db_session.expire_all()
    assert booking.status == model.BookingStatus.CANCELLED
    assert seat.status == model.ItemStatus.AVAILABLE
    assert payment.status == model.PaymentStatus.CANCELED


async def test_cancel_booking_pending_no_stripe_refund(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, seat_id, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    with patch("stripe.Refund.create") as mock_refund:
        cancel_res = await client.post(
            f"/bookings/{booking_id}/cancel",
            headers=user_headers,
            json={"reason": "Changed my mind before paying"}
        )

        assert cancel_res.status_code == 200
        assert float(cancel_res.json()["refund_amount"]) == 0.00
        mock_refund.assert_not_called()

    db_session.expire_all()
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    seat = db_session.query(model.InventoryItems).filter_by(id=seat_id).first()
    assert booking.status == model.BookingStatus.CANCELLED
    assert seat.status == model.ItemStatus.AVAILABLE


async def test_cancel_booking_after_event_started(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    service_id, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    # Move service start_time into the past
    service = db_session.query(model.Services).filter_by(id=service_id).first()
    service.start_time = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    res = await client.post(f"/bookings/{booking_id}/cancel", headers=user_headers)
    assert res.status_code == 400
    assert "already started" in res.json()["detail"]


async def test_cancel_booking_unauthorized_user(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    _, _, _, booking_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)

    # Seller tries to cancel Customer's booking -> 403 Forbidden
    res = await client.post(f"/bookings/{booking_id}/cancel", headers=seller_headers)
    assert res.status_code == 403
    assert "not authorized" in res.json()["detail"]
