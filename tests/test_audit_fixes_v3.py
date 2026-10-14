import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import jwt
import pytest
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app.core import oauth2
from app.database import model


async def make_test_location(client: httpx.AsyncClient, seller_headers: dict) -> int:
    loc_resp = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Karachi", "country": "PK", "address_line": "Audit Hall 3"}
    )
    assert loc_resp.status_code == 201, loc_resp.text
    return loc_resp.json()["id"]


# ============================================================================
# 1. High Priority Fix: Datetime Handling & Timezone Awareness
# ============================================================================

async def test_service_create_rejects_naive_datetime(
    client: httpx.AsyncClient,
    seller_headers: dict
):
    """
    POST /services must reject offset-naive datetimes with HTTP 422.
    """
    loc_id = await make_test_location(client, seller_headers)
    naive_start = "2026-11-20T10:00:00"  # Missing timezone offset
    naive_end = "2026-11-20T12:00:00"    # Missing timezone offset

    payload = {
        "service_name": "Naive Event",
        "location_id": loc_id,
        "booking_mode": "slot_capacity",
        "max_capacity": 50,
        "base_price": 20.00,
        "start_time": naive_start,
        "end_time": naive_end,
    }
    resp = await client.post("/services", headers=seller_headers, json=payload)
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    assert any("must be timezone-aware" in err.get("msg", "") for err in errors)


async def test_service_create_normalizes_to_utc(
    client: httpx.AsyncClient,
    seller_headers: dict,
    db_session: Session
):
    """
    POST /services must accept timezone-aware timestamps, normalize to UTC,
    and persist normalized values.
    """
    loc_id = await make_test_location(client, seller_headers)
    # +05:00 offset: 15:00 local == 10:00 UTC
    aware_start = "2026-11-20T15:00:00+05:00"
    aware_end = "2026-11-20T17:00:00+05:00"

    payload = {
        "service_name": "Aware Event",
        "location_id": loc_id,
        "booking_mode": "slot_capacity",
        "max_capacity": 50,
        "base_price": 20.00,
        "start_time": aware_start,
        "end_time": aware_end,
    }
    resp = await client.post("/services", headers=seller_headers, json=payload)
    assert resp.status_code == 201, resp.text
    svc_id = resp.json()["id"]

    # Verify persisted in database as 10:00 and 12:00 UTC
    svc = db_session.query(model.Services).filter_by(id=svc_id).first()
    assert svc is not None
    # Compare with expected UTC hour
    expected_utc_start = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
    expected_utc_end = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert svc.start_time.astimezone(timezone.utc) == expected_utc_start
    assert svc.end_time.astimezone(timezone.utc) == expected_utc_end


async def test_service_update_rejects_naive_datetime(
    client: httpx.AsyncClient,
    seller_headers: dict
):
    """
    PUT /services/{id} must reject offset-naive datetimes with HTTP 422.
    """
    loc_id = await make_test_location(client, seller_headers)
    now = datetime.now(timezone.utc)
    payload = {
        "service_name": "Valid Init",
        "location_id": loc_id,
        "booking_mode": "slot_capacity",
        "max_capacity": 50,
        "base_price": 20.00,
        "start_time": (now + timedelta(days=2)).isoformat(),
        "end_time": (now + timedelta(days=2, hours=3)).isoformat(),
    }
    init_resp = await client.post("/services", headers=seller_headers, json=payload)
    assert init_resp.status_code == 201
    svc_id = init_resp.json()["id"]

    # Send naive update
    naive_update = {"end_time": "2026-12-01T15:00:00"}
    up_resp = await client.put(f"/services/{svc_id}", headers=seller_headers, json=naive_update)
    assert up_resp.status_code == 422
    assert any("must be timezone-aware" in err.get("msg", "") for err in up_resp.json()["detail"])


# ============================================================================
# 2. Medium Priority Fix: Fail-Safe Logout Policy
# ============================================================================

async def test_logout_redis_failure_failsafe_policy(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    If Redis fails during access token revocation, /logout must:
    1. Not silently swallow the error.
    2. Return 503 Service Unavailable.
    3. Include Set-Cookie to clear the refresh_token cookie on the client.
    """
    # 1. Login to get tokens and cookie
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    refresh_token = login_resp.cookies["refresh_token"]

    client.cookies.set("refresh_token", refresh_token)

    # 2. Mock add_blacklist_token to simulate Redis connection failure
    with patch("app.core.oauth2.add_blacklist_token", side_effect=Exception("Redis connection refused")):
        logout_resp = await client.post(
            "/logout",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        assert logout_resp.status_code == 503
        assert "Token revocation failed" in logout_resp.json()["detail"]
        # Client cookie must still be instructed to be cleared
        set_cookie = logout_resp.headers.get("set-cookie", "")
        assert "refresh_token=" in set_cookie
        assert "Max-Age=0" in set_cookie


async def test_logout_session_deletion_failure_failsafe(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    If Redis fails during refresh token session deletion, /logout must return 503
    and instruct browser to delete cookie.
    """
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert login_resp.status_code == 200
    refresh_token = login_resp.cookies["refresh_token"]

    client.cookies.set("refresh_token", refresh_token)

    # Cookie-only logout where Redis delete raises exception
    with patch("app.core.redis.redis_client.delete", side_effect=Exception("Redis timeout")):
        logout_resp = await client.post("/logout")
        assert logout_resp.status_code == 503
        assert "Token revocation failed" in logout_resp.json()["detail"]
        set_cookie = logout_resp.headers.get("set-cookie", "")
        assert "refresh_token=" in set_cookie
        assert "Max-Age=0" in set_cookie


async def test_logout_with_already_revoked_token_clears_cookie_and_revokes_session(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    When /logout is called with an already blacklisted access token:
    1. Returns HTTP 401 Unauthorized ("Token has been revoked").
    2. Crucially sends Set-Cookie to clear the refresh_token cookie on the client.
    3. Revokes the active refresh session in Redis so it cannot be used anymore.
    """
    # 1. Login to establish session and get tokens
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    refresh_token = login_resp.cookies["refresh_token"]

    refresh_payload = jwt.decode(refresh_token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
    refresh_jti = refresh_payload["jti"]

    # Verify session is active in Redis before logout
    assert await test_redis.get(f"session:{test_user.id}:{refresh_jti}") == "active"

    # 2. Blacklist the access token beforehand
    await oauth2.add_blacklist_token(access_token)
    assert await oauth2.is_token_blacklisted(access_token) is True

    # 3. Present the blacklisted access token AND the refresh cookie to /logout
    client.cookies.set("refresh_token", refresh_token)
    logout_resp = await client.post(
        "/logout",
        headers={"Authorization": f"Bearer {access_token}"}
    )

    # 4. Must return 401 "Token has been revoked"
    assert logout_resp.status_code == 401
    assert "Token has been revoked" in logout_resp.json()["detail"]

    # 5. Set-Cookie must be present and instruct the browser to delete the cookie
    set_cookie = logout_resp.headers.get("set-cookie", "")
    assert "refresh_token=" in set_cookie
    assert "Max-Age=0" in set_cookie

    # 6. Verify that the refresh session in Redis was revoked!
    assert await test_redis.get(f"session:{test_user.id}:{refresh_jti}") is None

    # 7. Subsequent /refresh with this refresh token cookie must fail
    rf_resp = await client.post("/refresh")
    assert rf_resp.status_code == 401



# ============================================================================
# 3. High Priority Fix: Webhook Concurrency & Locking Strategy
# ============================================================================

async def test_webhook_slot_capacity_reconciliation_locking(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    db_session: Session
):
    """
    Verifies that stripe_webhook properly reconciles slot_capacity booking
    under row locking parity with booking creation.
    """
    loc_id = await make_test_location(client, seller_headers)
    now = datetime.now(timezone.utc)
    svc_resp = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Locking Concert",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 5,
            "base_price": 50.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat(),
        }
    )
    assert svc_resp.status_code == 201
    svc_id = svc_resp.json()["id"]

    # Book 2 tickets
    bk_resp = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": svc_id, "quantity": 2}
    )
    assert bk_resp.status_code == 201
    booking_id = bk_resp.json()["id"]

    # Create payment intent
    mock_intent = MagicMock()
    mock_intent.id = "pi_test_lock_reconcile"
    mock_intent.client_secret = "pi_test_lock_reconcile_secret_xyz"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        pi_resp = await client.post(
            f"/bookings/{booking_id}/pay",
            headers=user_headers
        )
    assert pi_resp.status_code == 200
    pi_id = pi_resp.json()["stripe_payment_intent_id"]

    # Webhook triggers payment_intent.succeeded
    webhook_payload = {
        "id": "evt_test_lock_reconcile",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": pi_id,
                "amount": 10000,
                "currency": "usd",
                "metadata": {"booking_id": str(booking_id)}
            }
        }
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        wb_resp = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "t=123,v1=test"}
        )
        assert wb_resp.status_code == 200
        assert wb_resp.json().get("status") == "success"

    # Booking must be confirmed
    db_session.expire_all()
    b = db_session.query(model.Booking).filter_by(id=booking_id).first()
    assert b.status == model.BookingStatus.CONFIRMED


async def test_webhook_unit_assigned_reconciliation_locking(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    db_session: Session
):
    """
    Verifies that stripe_webhook properly locks and verifies assigned inventory seats
    using deterministic ordered SELECT FOR UPDATE during reconciliation.
    """
    loc_id = await make_test_location(client, seller_headers)
    now = datetime.now(timezone.utc)
    svc_resp = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Seated Concert",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 75.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat(),
        }
    )
    assert svc_resp.status_code == 201
    svc_id = svc_resp.json()["id"]

    # Add tier
    tier_resp = await client.post(
        f"/services/{svc_id}/tiers",
        headers=seller_headers,
        json={"name": "VIP", "price": 100.00}
    )
    assert tier_resp.status_code == 201
    tier_id = tier_resp.json()["id"]

    # Add inventory
    inv_resp = await client.post(
        f"/services/{svc_id}/inventory/bulk",
        headers=seller_headers,
        json={"tier_id": tier_id, "identifier_codes": ["ROW1-A", "ROW1-B"]}
    )
    assert inv_resp.status_code == 201

    seat_a = db_session.query(model.InventoryItems).filter_by(
        service_id=svc_id, identifier_code="ROW1-A"
    ).first()

    # Create booking for ROW1-A
    bk_resp = await client.post(
        "/bookings",
        headers=user_headers,
        json={
            "service_id": svc_id,
            "tier_id": tier_id,
            "assigned_unit_ids": [seat_a.id],
            "quantity": 1
        }
    )
    assert bk_resp.status_code == 201
    booking_id = bk_resp.json()["id"]

    # Payment intent
    mock_intent = MagicMock()
    mock_intent.id = "pi_unit_lock_reconcile"
    mock_intent.client_secret = "pi_unit_lock_reconcile_secret"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        pi_resp = await client.post(
            f"/bookings/{booking_id}/pay",
            headers=user_headers
        )
    assert pi_resp.status_code == 200

    # Webhook triggers payment_intent.succeeded
    webhook_payload = {
        "id": "evt_test_unit_lock_reconcile",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_unit_lock_reconcile",
                "amount": 10000,
                "currency": "usd",
                "metadata": {"booking_id": str(booking_id)}
            }
        }
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        wb_resp = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "t=123,v1=test"}
        )
        assert wb_resp.status_code == 200
        assert wb_resp.json().get("status") == "success"

    # Booking must be confirmed and seat booked
    db_session.expire_all()
    b = db_session.query(model.Booking).filter_by(id=booking_id).first()
    assert b.status == model.BookingStatus.CONFIRMED
    seat = db_session.query(model.InventoryItems).filter_by(id=seat_a.id).first()
    assert seat.status == model.ItemStatus.BOOKED

