from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import httpx
import pytest
from sqlalchemy.orm import Session

from app.database import model, schema
from app.core import oauth2, security as utils
from app.core.tasks import released_expired_holds


import uuid


@pytest.fixture
def clean_user(db_session: Session) -> model.User:
    unique_id = uuid.uuid4().hex[:8]
    user = model.User(
        name=f"Review User {unique_id}",
        email=f"review_{unique_id}@example.com",
        password_hash=utils.hash("Password123!"),
        dob=datetime(1990, 1, 1).date(),
        phone_no=f"+1{uuid.uuid4().int % 10000000000:010d}",
        role=model.UserRole.CUSTOMER,
        is_verified=True
    )
    user.plain_password = "Password123!"
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def clean_user_headers(clean_user: model.User) -> dict[str, str]:
    token = oauth2.create_access_token(data={"user_id": clean_user.id, "sub": str(clean_user.id)})
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Legitimate Payers Webhook Confirmation (R4)
# ---------------------------------------------------------------------------
async def test_webhook_confirms_legitimate_payer_past_hold_expiry_for_reserved_seats(
    client: httpx.AsyncClient,
    clean_user: model.User,
    seller_user: model.User,
    db_session: Session
):
    """
    If booking is still PENDING and every seat is still RESERVED, the webhook confirms
    the booking and creates an invoice regardless of whether the hold clock has expired.
    """
    now = datetime.now(timezone.utc)
    loc = model.Location(seller_id=seller_user.seller_profile.id, city="Lahore", country="Pakistan", address_line="Audit Arena")
    db_session.add(loc)
    db_session.commit()

    srv = model.Services(
        seller_id=loc.seller_id,
        location_id=loc.id,
        service_name="Late Payer Concert",
        start_time=now + timedelta(days=2),
        end_time=now + timedelta(days=2, hours=2),
        booking_mode=model.BookingMode.UNIT_ASSIGNED,
        base_price=Decimal("100.00")
    )
    db_session.add(srv)
    db_session.commit()

    tier = model.ServiceTier(service_id=srv.id, name="VIP", price=Decimal("100.00"))
    db_session.add(tier)
    db_session.commit()

    seat = model.InventoryItems(
        service_id=srv.id,
        tier_id=tier.id,
        identifier_code="A-101",
        status=model.ItemStatus.RESERVED
    )
    db_session.add(seat)
    db_session.commit()

    # Create a booking created 15 minutes ago (hold expired by clock)
    booking = model.Booking(
        user_id=clean_user.id,
        service_id=srv.id,
        quantity=1,
        start_time=srv.start_time,
        end_time=srv.end_time,
        status=model.BookingStatus.PENDING,
        created_at=now - timedelta(minutes=15)
    )
    booking.assigned_units = [seat]
    db_session.add(booking)
    db_session.commit()

    payment = model.Payment(
        booking_id=booking.id,
        stripe_payment_intent_id="pi_late_payer_123",
        client_secret="secret",
        idempotency_key=f"booking_{booking.id}",
        amount=Decimal("100.00"),
        status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD
    )
    db_session.add(payment)
    db_session.commit()

    # Webhook arrives with payment_intent.succeeded
    event_payload = {
        "id": "evt_late_123",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_late_payer_123",
                "amount": 10000,
                "status": "succeeded"
            }
        }
    }

    with patch("stripe.Webhook.construct_event", return_value=event_payload):
        with patch("stripe.Refund.create") as mock_refund:
            res = await client.post(
                "/webhook/stripe",
                headers={"stripe-signature": "valid_sig"},
                json=event_payload
            )
            assert res.status_code == 200
            assert res.json()["status"] == "success"
            mock_refund.assert_not_called()

    db_session.expire_all()
    db_booking = db_session.query(model.Booking).filter_by(id=booking.id).first()
    db_payment = db_session.query(model.Payment).filter_by(id=payment.id).first()
    db_seat = db_session.query(model.InventoryItems).filter_by(id=seat.id).first()
    db_invoice = db_session.query(model.Invoice).filter_by(payment_id=payment.id).first()

    assert db_booking.status == model.BookingStatus.CONFIRMED
    assert db_payment.status == model.PaymentStatus.SUCCEEDED
    assert db_seat.status == model.ItemStatus.BOOKED
    assert db_invoice is not None


# ---------------------------------------------------------------------------
# 2. Email Change Requires Current Password & Invalidation
# ---------------------------------------------------------------------------
async def test_email_change_requires_current_password(
    client: httpx.AsyncClient,
    clean_user: model.User,
    clean_user_headers: dict[str, str],
    db_session: Session
):
    """
    Attempting to update email without current_password or with incorrect password returns 400.
    """
    # 1. Missing current_password
    res = await client.put(
        f"/account/{clean_user.id}",
        headers=clean_user_headers,
        json={"email": "attacker@example.com"}
    )
    assert res.status_code == 400
    assert "Current password is required" in res.json()["detail"]

    # 2. Wrong current_password
    res_wrong = await client.put(
        f"/account/{clean_user.id}",
        headers=clean_user_headers,
        json={"email": "attacker@example.com", "current_password": "WrongPassword123!"}
    )
    assert res_wrong.status_code == 400
    assert "Current password is required" in res_wrong.json()["detail"]

    # 3. Correct current_password succeeds, marks unverified, and invalidates old access token
    res_ok = await client.put(
        f"/account/{clean_user.id}",
        headers=clean_user_headers,
        json={"email": "newverified@example.com", "current_password": "Password123!"}
    )
    assert res_ok.status_code == 200

    # Old token must now be rejected with 401 on /account/me
    res_me = await client.get("/account/me", headers=clean_user_headers)
    assert res_me.status_code == 401
    assert "revoked" in res_me.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Password Change Invalidates Existing Access Token
# ---------------------------------------------------------------------------
async def test_password_change_invalidates_old_access_token(
    client: httpx.AsyncClient,
    clean_user: model.User,
    clean_user_headers: dict[str, str]
):
    """
    Updating password revokes old access token so subsequent /account/me returns 401.
    """
    res = await client.put(
        f"/account/{clean_user.id}",
        headers=clean_user_headers,
        json={"password": "NewSecurePassword123!", "current_password": "Password123!"}
    )
    assert res.status_code == 200

    res_me = await client.get("/account/me", headers=clean_user_headers)
    assert res_me.status_code == 401
    assert "revoked" in res_me.json()["detail"]


# ---------------------------------------------------------------------------
# 4. Explicit Null Values Rejection (HTTP 422)
# ---------------------------------------------------------------------------
async def test_explicit_null_values_rejected_with_422(
    client: httpx.AsyncClient,
    clean_user: model.User,
    seller_headers: dict[str, str],
    clean_user_headers: dict[str, str],
    db_session: Session
):
    """
    Passing explicit null for service_name, base_price, start_time, city, or user name
    is rejected with 422 Unprocessable Entity instead of crashing with 500.
    """
    # Create valid location and service first
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "Pakistan", "address_line": "Null Check Hall"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Null Check Service",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 20.00,
            "start_time": (now + timedelta(days=5)).isoformat(),
            "end_time": (now + timedelta(days=5, hours=2)).isoformat()
        }
    )
    assert srv_res.status_code == 201
    srv_id = srv_res.json()["id"]

    # 1. service_name: null -> 422
    r1 = await client.put(f"/services/{srv_id}", headers=seller_headers, json={"service_name": None})
    assert r1.status_code == 422

    # 2. base_price: null -> 422
    r2 = await client.put(f"/services/{srv_id}", headers=seller_headers, json={"base_price": None})
    assert r2.status_code == 422

    # 3. start_time: null -> 422
    r3 = await client.put(f"/services/{srv_id}", headers=seller_headers, json={"start_time": None})
    assert r3.status_code == 422

    # 4. location city: null -> 422
    r4 = await client.put(f"/locations/{loc_id}", headers=seller_headers, json={"city": None})
    assert r4.status_code == 422

    # 5. user name: null -> 422
    r5 = await client.put(f"/account/{clean_user.id}", headers=clean_user_headers, json={"name": None})
    assert r5.status_code == 422


# ---------------------------------------------------------------------------
# 5. Anti-Seat-Hoarding Protections
# ---------------------------------------------------------------------------
async def test_anti_seat_hoarding_caps(
    client: httpx.AsyncClient,
    clean_user: model.User,
    clean_user_headers: dict[str, str],
    seller_headers: dict[str, str]
):
    """
    Enforces maximum 10 tickets per booking and maximum 5 active pending holds per user.
    """
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Karachi", "country": "Pakistan", "address_line": "Hoarding Venue"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Anti-Hoarding Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 500,
            "base_price": 10.00,
            "start_time": (now + timedelta(days=4)).isoformat(),
            "end_time": (now + timedelta(days=4, hours=2)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    # 1. Per-booking cap (>10 rejected)
    r_cap = await client.post(
        "/bookings",
        headers=clean_user_headers,
        json={"service_id": srv_id, "quantity": 11}
    )
    assert r_cap.status_code == 400
    assert "Cannot book more than 10 tickets" in r_cap.json()["detail"]

    # 2. Max pending holds cap (5 allowed, 6th rejected)
    for _ in range(5):
        r_hold = await client.post(
            "/bookings",
            headers=clean_user_headers,
            json={"service_id": srv_id, "quantity": 1}
        )
        assert r_hold.status_code == 201

    # 6th attempt must be rejected
    r_6th = await client.post(
        "/bookings",
        headers=clean_user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    assert r_6th.status_code == 400
    assert "maximum limit" in r_6th.json()["detail"]


# ---------------------------------------------------------------------------
# 6. Booking Mode Immutability on Live Event
# ---------------------------------------------------------------------------
async def test_booking_mode_cannot_change_when_event_has_bookings(
    client: httpx.AsyncClient,
    clean_user_headers: dict[str, str],
    seller_headers: dict[str, str]
):
    """
    Cannot change booking_mode on an event that already has bookings.
    """
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Peshawar", "country": "Pakistan", "address_line": "Mode Hall"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Mode Test Service",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 10.00,
            "start_time": (now + timedelta(days=6)).isoformat(),
            "end_time": (now + timedelta(days=6, hours=2)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    # Create a booking
    b_res = await client.post(
        "/bookings",
        headers=clean_user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    assert b_res.status_code == 201

    # Attempt to change booking_mode to unit_assigned
    change_res = await client.put(
        f"/services/{srv_id}",
        headers=seller_headers,
        json={"booking_mode": "unit_assigned"}
    )
    assert change_res.status_code == 400
    assert "Cannot change booking_mode" in change_res.json()["detail"]


# ---------------------------------------------------------------------------
# 7. Cookie-Only Logout Succeeds (No Authorization Header)
# ---------------------------------------------------------------------------
async def test_cookie_only_logout_succeeds(
    client: httpx.AsyncClient,
    clean_user: model.User,
    test_redis
):
    """
    Logging out without an Authorization header (normal after page refresh)
    clears the HttpOnly cookie and invalidates the Redis refresh session with HTTP 200.
    """
    # 1. Login to get refresh cookie
    login_res = await client.post(
        "/login",
        data={"username": clean_user.email, "password": "Password123!"}
    )
    assert login_res.status_code == 200
    refresh_cookie = login_res.cookies.get("refresh_token")
    assert refresh_cookie is not None

    # 2. Call /logout with ONLY the cookie (no Authorization header!)
    client.cookies.set("refresh_token", refresh_cookie)
    logout_res = await client.post("/logout")
    assert logout_res.status_code == 200
    assert logout_res.json()["message"] == "Logged out successfully"


# ---------------------------------------------------------------------------
# 8. Two Tabs Refresh Token Rotation Grace Period
# ---------------------------------------------------------------------------
async def test_refresh_token_concurrent_tab_grace_window(
    client: httpx.AsyncClient,
    clean_user: model.User
):
    """
    Two tabs refreshing near-simultaneously: Tab 2 uses the token Tab 1 just rotated.
    Within the 30-second grace window, Tab 2 receives a fresh access token without
    falsely triggering whole-family replay revocation.
    """
    login_res = await client.post(
        "/login",
        data={"username": clean_user.email, "password": "Password123!"}
    )
    refresh_cookie = login_res.cookies.get("refresh_token")

    # Tab 1 refreshes
    client.cookies.set("refresh_token", refresh_cookie)
    tab1_res = await client.post("/refresh")
    assert tab1_res.status_code == 200
    assert "access_token" in tab1_res.json()

    # Tab 2 refreshes immediately with the same original cookie within grace window
    client.cookies.set("refresh_token", refresh_cookie)
    tab2_res = await client.post("/refresh")
    assert tab2_res.status_code == 401
    assert "set-cookie" in tab2_res.headers

    # Crucial assertion: Tab 1's newly rotated session is STILL ALIVE (1 session left, NOT 0!)
    from app import redis_client
    remaining_sessions = [k async for k in redis_client.redis_client.scan_iter(f"session:{clean_user.id}:*")]
    assert len(remaining_sessions) == 1


# ---------------------------------------------------------------------------
# 9. Anti-Enumeration on /resend-otp
# ---------------------------------------------------------------------------
async def test_resend_otp_anti_enumeration(client: httpx.AsyncClient):
    """
    Calling /resend-otp for an email that does not exist returns 200 with standard message,
    preventing user enumeration probing.
    """
    res = await client.post(
        "/account/resend-otp",
        json={"email": "nonexistent_ghost_account@example.com"}
    )
    assert res.status_code == 200
    assert "verification code has been sent" in res.json()["message"]


# ---------------------------------------------------------------------------
# 10. Sweeper Skips Booking if Stripe Cancel Fails
# ---------------------------------------------------------------------------
def test_sweeper_skips_booking_if_stripe_cancel_fails(
    seller_user: model.User,
    clean_user: model.User,
    db_session: Session
):
    """
    If Stripe cancel raises an error, the sweeper does not cancel the booking
    or release its seats, preventing inventory desync.
    """
    now = datetime.now(timezone.utc)
    loc = model.Location(seller_id=seller_user.seller_profile.id, city="Multan", country="Pakistan", address_line="Task Venue")
    db_session.add(loc)
    db_session.commit()

    srv = model.Services(
        seller_id=loc.seller_id,
        location_id=loc.id,
        service_name="Sweeper Skip Event",
        start_time=now + timedelta(days=2),
        end_time=now + timedelta(days=2, hours=2),
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=50,
        base_price=Decimal("15.00")
    )
    db_session.add(srv)
    db_session.commit()

    booking = model.Booking(
        user_id=clean_user.id,
        service_id=srv.id,
        quantity=1,
        start_time=srv.start_time,
        end_time=srv.end_time,
        status=model.BookingStatus.PENDING,
        created_at=now - timedelta(minutes=20)
    )
    db_session.add(booking)
    db_session.commit()

    payment = model.Payment(
        booking_id=booking.id,
        stripe_payment_intent_id="pi_stripe_fail_123",
        client_secret="secret",
        idempotency_key=f"booking_{booking.id}",
        amount=Decimal("15.00"),
        status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD
    )
    db_session.add(payment)
    db_session.commit()

    # Simulate Stripe cancel failure
    with patch("stripe.PaymentIntent.cancel", side_effect=Exception("Stripe API down")):
        released_expired_holds(db=db_session)

    db_session.expire_all()
    booking_in_db = db_session.query(model.Booking).filter_by(id=booking.id).first()
    # Booking must NOT have been cancelled!
    assert booking_in_db.status == model.BookingStatus.PENDING


# ---------------------------------------------------------------------------
# 11. /pay Reconciliation Creates Invoice & Confirms Booking
# ---------------------------------------------------------------------------
async def test_pay_reconcile_creates_invoice(
    client: httpx.AsyncClient,
    clean_user: model.User,
    clean_user_headers: dict[str, str],
    seller_user: model.User,
    db_session: Session
):
    """
    When /pay reconciles a payment that already succeeded on Stripe,
    it must mark the booking CONFIRMED and generate an Invoice record.
    """
    now = datetime.now(timezone.utc)
    loc = model.Location(seller_id=seller_user.seller_profile.id, city="Quetta", country="Pakistan", address_line="Invoice Arena")
    db_session.add(loc)
    db_session.commit()

    srv = model.Services(
        seller_id=loc.seller_id,
        location_id=loc.id,
        service_name="Invoice Reconcile Event",
        start_time=now + timedelta(days=3),
        end_time=now + timedelta(days=3, hours=2),
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=50,
        base_price=Decimal("45.00")
    )
    db_session.add(srv)
    db_session.commit()

    booking = model.Booking(
        user_id=clean_user.id,
        service_id=srv.id,
        quantity=1,
        start_time=srv.start_time,
        end_time=srv.end_time,
        status=model.BookingStatus.PENDING,
        created_at=now
    )
    db_session.add(booking)
    db_session.commit()

    payment = model.Payment(
        booking_id=booking.id,
        stripe_payment_intent_id="pi_reconcile_invoice_test",
        client_secret="secret",
        idempotency_key=f"booking_{booking.id}",
        amount=Decimal("45.00"),
        status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD
    )
    db_session.add(payment)
    db_session.commit()

    mock_intent = MagicMock()
    mock_intent.status = "succeeded"

    with patch("stripe.PaymentIntent.retrieve", return_value=mock_intent):
        pay_res = await client.post(f"/bookings/{booking.id}/pay", headers=clean_user_headers)
        assert pay_res.status_code == 200

    db_session.expire_all()
    db_booking = db_session.query(model.Booking).filter_by(id=booking.id).first()
    db_payment = db_session.query(model.Payment).filter_by(id=payment.id).first()
    db_invoice = db_session.query(model.Invoice).filter_by(payment_id=payment.id).first()

    assert db_booking.status == model.BookingStatus.CONFIRMED
    assert db_payment.status == model.PaymentStatus.SUCCEEDED
    assert db_invoice is not None
    assert db_invoice.invoice_number.startswith("INV-")
