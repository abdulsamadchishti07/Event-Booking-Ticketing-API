import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app.core import oauth2
from app.database import model


# ============================================================================
# 1. reset_password Unlocks Per-IP Lockouts and Case-Insensitive Lookup
# ============================================================================

async def test_reset_password_unlocks_per_ip_lockout_and_case_insensitive(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    1. Simulates 5 failed logins creating a per-IP lockout: account_locked:{email}:{ip}.
    2. Calls /account/forgot-password and /account/reset-password with uppercase email.
    3. Verifies that per-IP lockout is wiped and user can immediately log in with new password.
    """
    email_clean = test_user.email.strip().lower()
    client_ip = "127.0.0.1"

    # Simulate IP-level lockout
    lock_key = f"account_locked:{email_clean}:{client_ip}"
    failed_key = f"failed_logins:{email_clean}:{client_ip}"
    await test_redis.set(lock_key, "locked", ex=900)
    await test_redis.set(failed_key, "5", ex=900)

    # 1. Request forgot password OTP
    fp_res = await client.post("/account/forgot-password", json={"email": test_user.email.upper()})
    assert fp_res.status_code == 200

    otp = await test_redis.get(f"reset_pwd_otp:{email_clean}")
    assert otp is not None

    # 2. Reset password with mixed-case email
    new_password = "BrandNewSecretPassword123!"
    rp_res = await client.post(
        "/account/reset-password",
        json={
            "email": test_user.email.upper(),
            "otp": otp,
            "new_password": new_password
        }
    )
    assert rp_res.status_code == 200

    # 3. Verify per-IP keys are cleared
    assert await test_redis.get(lock_key) is None
    assert await test_redis.get(failed_key) is None

    # 4. User can immediately log in with new password without lockout
    login_res = await client.post(
        "/login",
        data={"username": test_user.email, "password": new_password}
    )
    assert login_res.status_code == 200
    assert "access_token" in login_res.json()


# ============================================================================
# 2. stripe_webhook Ignores Expired PENDING Holds in slot_capacity
# ============================================================================

async def test_stripe_webhook_slot_capacity_ignores_expired_pending_holds(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    db_session: Session
):
    """
    When reconciling slot_capacity, expired PENDING holds (older than 10 mins)
    must NOT count against remaining capacity.
    """
    # 1. Location & Event with capacity = 2
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "PK", "address_line": "Arena 1"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Tight Capacity Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 2,
            "base_price": 50.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat(),
        }
    )
    assert svc_res.status_code == 201
    svc_id = svc_res.json()["id"]

    # 2. Create an EXPIRED hold for User A (quantity = 1, created 20 minutes ago)
    expired_booking = model.Booking(
        service_id=svc_id,
        user_id=1,  # arbitrary user id
        quantity=1,
        status=model.BookingStatus.PENDING,
        created_at=now - timedelta(minutes=20),
        start_time=now + timedelta(days=2),
        end_time=now + timedelta(days=2, hours=3)
    )
    db_session.add(expired_booking)
    db_session.commit()

    # 3. User B books remaining capacity of 2 (create_booking allows it because hold is expired)
    book_b = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": svc_id, "quantity": 2}
    )
    assert book_b.status_code == 201
    booking_b_id = book_b.json()["id"]

    # 4. Create payment intent for User B
    mock_intent = MagicMock()
    mock_intent.id = "pi_user_b_success"
    mock_intent.client_secret = "pi_user_b_secret"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        pi_resp = await client.post(f"/bookings/{booking_b_id}/pay", headers=user_headers)
        assert pi_resp.status_code == 200

    # 5. Webhook payment_intent.succeeded arrives for User B
    webhook_payload = {
        "id": "evt_user_b_reconcile",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_user_b_success",
                "amount": 10000,
                "currency": "usd",
                "metadata": {"booking_id": str(booking_b_id)}
            }
        }
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        wb_resp = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "sig"}
        )
        assert wb_resp.status_code == 200

    # 6. User B's booking must be CONFIRMED (NOT refunded/cancelled!)
    db_session.expire_all()
    b = db_session.query(model.Booking).filter_by(id=booking_b_id).first()
    assert b.status == model.BookingStatus.CONFIRMED


# ============================================================================
# 3. cancel_booking_and_refund Cancels Failed Stripe PaymentIntents
# ============================================================================

async def test_cancel_booking_cancels_stripe_payment_intent_when_failed(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    db_session: Session
):
    """
    Cancelling a pending booking whose payment status is FAILED (or REQUIRES_PAYMENT_METHOD)
    must cancel the PaymentIntent on Stripe and update payment status to CANCELED.
    """
    # 1. Setup service and booking
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Peshawar", "country": "PK", "address_line": "Hall A"}
    )
    loc_id = loc_res.json()["id"]
    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Refund Intent Test",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 10,
            "base_price": 25.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat(),
        }
    )
    svc_id = svc_res.json()["id"]

    bk_res = await client.post("/bookings", headers=user_headers, json={"service_id": svc_id, "quantity": 1})
    assert bk_res.status_code == 201
    booking_id = bk_res.json()["id"]

    # 2. Attach payment record with status FAILED
    payment = model.Payment(
        booking_id=booking_id,
        stripe_payment_intent_id="pi_failed_intent_123",
        client_secret="secret",
        idempotency_key="key",
        amount=Decimal("25.00"),
        status=model.PaymentStatus.FAILED
    )
    db_session.add(payment)
    db_session.commit()

    # 3. Cancel the booking
    with patch("stripe.PaymentIntent.cancel") as mock_cancel:
        cancel_res = await client.post(f"/bookings/{booking_id}/cancel", headers=user_headers)
        assert cancel_res.status_code == 200
        mock_cancel.assert_called_once_with("pi_failed_intent_123")

    db_session.expire_all()
    p = db_session.query(model.Payment).filter_by(booking_id=booking_id).first()
    assert p.status == model.PaymentStatus.CANCELED


# ============================================================================
# 4. update_location Invalidates Services Discovery Cache
# ============================================================================

async def test_update_location_invalidates_services_cache(
    client: httpx.AsyncClient,
    seller_headers: dict
):
    """
    Updating a location's city/country via PUT /locations/{id} must invalidate
    the public services discovery cache (GET /services).
    """
    # 1. Create location and service
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Quetta", "country": "PK", "address_line": "Venue 1"}
    )
    loc_id = loc_res.json()["id"]
    now = datetime.now(timezone.utc)
    await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Cached Venue Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 10.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat(),
        }
    )

    # 2. Cache query with city=Quetta
    r1 = await client.get("/services?city=Quetta")
    assert r1.status_code == 200
    assert r1.headers.get("X-Cache") == "MISS"
    assert len(r1.json()) >= 1

    r2 = await client.get("/services?city=Quetta")
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "HIT"

    # 3. Update location city to Gwadar
    up_res = await client.put(f"/locations/{loc_id}", headers=seller_headers, json={"city": "Gwadar"})
    assert up_res.status_code == 200

    # 4. Searching for Quetta must miss cache and return empty
    r3 = await client.get("/services?city=Quetta")
    assert r3.status_code == 200
    assert r3.headers.get("X-Cache") == "MISS"
    assert len(r3.json()) == 0

    # 5. Searching for Gwadar must return the event
    r4 = await client.get("/services?city=Gwadar")
    assert r4.status_code == 200
    assert any(item["service_name"] == "Cached Venue Event" for item in r4.json())
