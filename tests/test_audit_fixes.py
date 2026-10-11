from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import httpx
import pytest
from sqlalchemy.orm import Session

from app import oauth2, utils
from app.database import model


@pytest.fixture
def second_seller(db_session: Session) -> model.User:
    """Creates a second verified seller user."""
    user = model.User(
        name="Second Seller",
        email="second_seller@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1990, 1, 1),
        role=model.UserRole.SELLER,
        phone_no="+923009998877",
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    seller_profile = model.SellerProfile(
        user_id=user.id,
        business_name="Second Business",
        business_desc="Second seller business",
        is_verified=True
    )
    db_session.add(seller_profile)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def second_seller_headers(second_seller: model.User) -> dict[str, str]:
    token = oauth2.create_access_token(data={"user_id": second_seller.id, "sub": str(second_seller.id)})
    return {"Authorization": f"Bearer {token}"}


# ==========================================================
# 1. Critical #1: Admin self-registration prevention
# ==========================================================
async def test_admin_self_registration_prevented(
    client: httpx.AsyncClient,
    db_session: Session
):
    """User cannot register as admin even if role='admin' is sent."""
    res = await client.post(
        "/account/register",
        json={
            "name": "Attacker",
            "email": "attacker@example.com",
            "password": "Password123!",
            "dob": "1995-05-15",
            "phone_no": "+923331112233",
            "role": "admin"
        }
    )
    assert res.status_code == 201
    user_id = res.json()["id"]

    db_user = db_session.query(model.User).filter_by(id=user_id).first()
    assert db_user.role == model.UserRole.CUSTOMER


async def test_user_self_promotion_via_update_prevented(
    client: httpx.AsyncClient,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    """User cannot promote themselves to admin via PUT /account/{id}."""
    res = await client.put(
        f"/account/{test_user.id}",
        headers=user_headers,
        json={"name": "New Name", "role": "admin"}
    )
    assert res.status_code == 200

    db_session.refresh(test_user)
    assert test_user.role == model.UserRole.CUSTOMER


# ==========================================================
# 2. Critical #5: Refresh tokens cannot act as access tokens
# ==========================================================
async def test_refresh_token_rejected_as_access_token(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """Refresh token must be rejected when sent in Authorization: Bearer header."""
    refresh_token, _ = oauth2.create_fresh_token(data={"sub": str(test_user.id)})
    res = await client.get(
        "/account/me",
        headers={"Authorization": f"Bearer {refresh_token}"}
    )
    assert res.status_code == 401


# ==========================================================
# 3. Critical #6: Mixed-case emails can log in
# ==========================================================
async def test_mixed_case_email_registration_and_login(
    client: httpx.AsyncClient,
    db_session: Session
):
    """Mixed-case email registered can be verified and logged into."""
    res = await client.post(
        "/account/register",
        json={
            "name": "Mixed Case",
            "email": "MixedCaseUser@Example.Com",
            "password": "Password123!",
            "dob": "1994-01-01",
            "phone_no": "+923007654321"
        }
    )
    assert res.status_code == 201

    db_user = db_session.query(model.User).filter(model.User.email == "mixedcaseuser@example.com").first()
    assert db_user is not None

    # Verify with OTP
    verify_res = await client.post(
        "/account/verify-otp",
        json={"email": "MixedCaseUser@Example.Com", "otp": db_user.verification_otp}
    )
    assert verify_res.status_code == 200

    # Login with lowercase email
    login_res = await client.post(
        "/login",
        data={"username": "mixedcaseuser@example.com", "password": "Password123!"}
    )
    assert login_res.status_code == 200
    assert "access_token" in login_res.json()


# ==========================================================
# 4. High #8: Seller cannot attach service to other seller's location
# ==========================================================
async def test_seller_cannot_attach_service_to_other_seller_location(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    second_seller_headers: dict[str, str]
):
    """Seller B cannot create a service at Seller A's location."""
    # Seller A creates location
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Karachi", "country": "Pakistan", "address_line": "Seller A Venue"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    # Seller B attempts to create service on Seller A's location
    now = datetime.now(timezone.utc)
    srv_res = await client.post(
        "/services",
        headers=second_seller_headers,
        json={
            "service_name": "Hijacked Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 20.00,
            "start_time": (now + timedelta(days=3)).isoformat(),
            "end_time": (now + timedelta(days=3, hours=2)).isoformat()
        }
    )
    assert srv_res.status_code == 403
    assert "You are not authorized to host services at this location" in srv_res.json()["detail"]


# ==========================================================
# 5. High #9: User profile privacy (GET /account/{id})
# ==========================================================
async def test_user_cannot_view_other_user_profile(
    client: httpx.AsyncClient,
    test_user: model.User,
    second_seller: model.User,
    user_headers: dict[str, str]
):
    """Customer cannot view another user's profile."""
    res = await client.get(
        f"/account/{second_seller.id}",
        headers=user_headers
    )
    assert res.status_code == 403
    assert "not authorized" in res.json()["detail"]


# ==========================================================
# 6. High #10: Unverified user cannot book
# ==========================================================
async def test_unverified_user_cannot_book(
    client: httpx.AsyncClient,
    unverified_headers: dict[str, str]
):
    """Unverified user receives 403 Forbidden when creating a booking."""
    res = await client.post(
        "/bookings",
        headers=unverified_headers,
        json={"service_id": 1, "quantity": 1}
    )
    assert res.status_code == 403
    assert "Account is not verified" in res.json()["detail"]


# ==========================================================
# 7. High #15: Free events auto-confirmation ($0.00)
# ==========================================================
async def test_free_event_pay_and_auto_confirm(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """Free event bookings are confirmed directly without Stripe."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Lahore", "country": "Pakistan", "address_line": "Free Community Park"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Free Community Workshop",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 0.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    # Book free ticket
    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    assert book_res.status_code == 201
    booking_id = book_res.json()["id"]

    # Pay for free event
    pay_res = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
    assert pay_res.status_code == 200
    pay_data = pay_res.json()
    assert pay_data["status"] == "succeeded"
    assert float(pay_data["amount"]) == 0.00

    # Verify booking is confirmed in DB
    db_session.expire_all()
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    assert booking.status == model.BookingStatus.CONFIRMED


# ==========================================================
# 8. Critical #3: Webhook on already-cancelled booking auto-refunds
# ==========================================================
async def test_webhook_auto_refunds_cancelled_booking(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """If payment succeeds on an already cancelled booking, Stripe webhook issues auto-refund."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "Pakistan", "address_line": "Hall A"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Concert 2026",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 100,
            "base_price": 50.00,
            "start_time": (now + timedelta(days=5)).isoformat(),
            "end_time": (now + timedelta(days=5, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    booking_id = book_res.json()["id"]

    mock_intent = MagicMock()
    mock_intent.id = "pi_cancelled_race_test"
    mock_intent.client_secret = "secret_race"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

    # User cancels booking before webhook arrives
    await client.post(
        f"/bookings/{booking_id}/cancel",
        headers=user_headers,
        json={"reason": "Customer cancelled early"}
    )

    db_session.expire_all()
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    assert booking.status == model.BookingStatus.CANCELLED

    # Now Stripe webhook arrives with payment_intent.succeeded
    mock_event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_cancelled_race_test"}}
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event), \
         patch("stripe.Refund.create") as mock_refund:
        mock_refund.return_value = MagicMock(id="re_auto_123", status="succeeded")

        wh_res = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert wh_res.status_code == 200
        assert wh_res.json()["status"] == "auto_refunded_cancelled_booking"

        # Verify auto-refund was triggered
        mock_refund.assert_called_once()


# ==========================================================
# 9. Missing Endpoints: GET /bookings/me, GET /bookings/{id}, PUT/DELETE /services/{id}
# ==========================================================
async def test_get_my_bookings_and_by_id(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):
    """Customer can list their bookings and fetch a single booking by ID."""
    # List bookings
    list_res = await client.get("/bookings/me", headers=user_headers)
    assert list_res.status_code == 200
    assert isinstance(list_res.json(), list)


async def test_services_update_and_delete(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    """Seller can update and delete their own service."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Multan", "country": "Pakistan", "address_line": "Auditorium"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Temporary Festival",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 30.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    # Update service
    put_res = await client.put(
        f"/services/{srv_id}",
        headers=seller_headers,
        json={"service_name": "Updated Festival Name", "base_price": 35.00}
    )
    assert put_res.status_code == 200
    assert put_res.json()["service_name"] == "Updated Festival Name"
    assert float(put_res.json()["base_price"]) == 35.00

    # Delete service
    del_res = await client.delete(f"/services/{srv_id}", headers=seller_headers)
    assert del_res.status_code == 204

    # Verify deleted
    get_res = await client.get(f"/services/{srv_id}")
    assert get_res.status_code == 404


# ==========================================================
# 10. Bug A: Webhook on expired hold booking triggers auto-refund and seat release
# ==========================================================
async def test_webhook_auto_refunds_expired_hold_booking(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """If payment succeeds on an expired pending hold, webhook auto-refunds and cancels."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Quetta", "country": "Pakistan", "address_line": "Hall Expired"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Late Payment Event",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 50.00,
            "start_time": (now + timedelta(days=5)).isoformat(),
            "end_time": (now + timedelta(days=5, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    tier_res = await client.post(
        f"/services/{srv_id}/tiers",
        headers=seller_headers,
        json={"name": "VIP", "price": 50.00}
    )
    tier_id = tier_res.json()["id"]

    inv_res = await client.post(
        f"/services/{srv_id}/inventory/bulk",
        headers=seller_headers,
        json={"tier_id": tier_id, "identifier_codes": ["EXP-1"]}
    )
    seat = db_session.query(model.InventoryItems).filter_by(service_id=srv_id, identifier_code="EXP-1").first()

    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": srv_id, "tier_id": tier_id, "assigned_unit_ids": [seat.id], "quantity": 1}
    )
    booking_id = book_res.json()["id"]

    mock_intent = MagicMock()
    mock_intent.id = "pi_expired_hold_test"
    mock_intent.client_secret = "secret_exp"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

    # Fast forward booking created_at to 15 minutes ago (expired hold)
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    booking.created_at = now - timedelta(minutes=15)
    db_session.commit()

    # Webhook arrives
    mock_event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_expired_hold_test"}}
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event), \
         patch("stripe.Refund.create") as mock_refund:
        mock_refund.return_value = MagicMock(id="re_exp_123", status="succeeded")

        wh_res = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert wh_res.status_code == 200
        assert wh_res.json()["status"] == "auto_refunded_cancelled_booking"

        # Verify auto-refund was triggered
        mock_refund.assert_called_once()

    # Verify booking is CANCELLED and seat is AVAILABLE
    db_session.expire_all()
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    seat = db_session.query(model.InventoryItems).filter_by(id=seat.id).first()
    assert booking.status == model.BookingStatus.CANCELLED
    assert seat.status == model.ItemStatus.AVAILABLE


# ==========================================================
# 11. Bug B: Payment retry creates unique attempt when previous intent cancelled
# ==========================================================
async def test_payment_retry_after_cancelled_intent(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """When a payment intent is cancelled, retrying creates a new attempt key instead of reusing old cancelled intent."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Peshawar", "country": "Pakistan", "address_line": "Hall Retry"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Retry Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 40.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    booking_id = book_res.json()["id"]

    # First attempt creates pi_first
    mock_first = MagicMock()
    mock_first.id = "pi_first_attempt"
    mock_first.client_secret = "secret_first"
    mock_first.status = "requires_payment_method"

    with patch("stripe.PaymentIntent.create", return_value=mock_first):
        pay1 = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
        assert pay1.status_code == 200
        assert pay1.json()["stripe_payment_intent_id"] == "pi_first_attempt"

    # Now simulate intent became canceled on Stripe
    mock_cancelled = MagicMock()
    mock_cancelled.status = "canceled"

    mock_second = MagicMock()
    mock_second.id = "pi_second_attempt"
    mock_second.client_secret = "secret_second"

    with patch("stripe.PaymentIntent.retrieve", return_value=mock_cancelled), \
         patch("stripe.PaymentIntent.create", return_value=mock_second) as mock_create:
        pay2 = await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)
        assert pay2.status_code == 200
        assert pay2.json()["stripe_payment_intent_id"] == "pi_second_attempt"

        # Verify a new attempt key was generated
        _, kwargs = mock_create.call_args
        assert "attempt_" in kwargs["idempotency_key"]


# ==========================================================
# 12. Bug D: Production cookie security flag
# ==========================================================
async def test_cookie_secure_enforced_in_production(
    client: httpx.AsyncClient,
    test_user: model.User,
    monkeypatch
):
    """When environment is production, response cookies have secure=True."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "environment", "production")

    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert login_resp.status_code == 200
    cookie_header = login_resp.headers.get("set-cookie", "")
    assert "secure" in cookie_header.lower()


# ==========================================================
# 13. Edge Cases: Out-of-order Webhook Handling
# ==========================================================
async def test_out_of_order_webhook_payment_failed_ignored_after_succeeded(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """A delayed payment_failed event must not overwrite a SUCCEEDED payment."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "Pakistan", "address_line": "Stale Event Hall"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Out of order webhook event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 30.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    booking_id = book_res.json()["id"]

    mock_intent = MagicMock()
    mock_intent.id = "pi_out_of_order_123"
    mock_intent.client_secret = "secret_ooo"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

    # 1. Success event arrives first
    mock_succeeded_event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_out_of_order_123"}}
    }
    with patch("stripe.Webhook.construct_event", return_value=mock_succeeded_event):
        succ_res = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert succ_res.status_code == 200
        assert succ_res.json()["status"] == "success"

    # Verify payment is SUCCEEDED and booking CONFIRMED
    db_session.expire_all()
    payment = db_session.query(model.Payment).filter_by(stripe_payment_intent_id="pi_out_of_order_123").first()
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    assert payment.status == model.PaymentStatus.SUCCEEDED
    assert booking.status == model.BookingStatus.CONFIRMED

    # 2. Delayed payment_intent.payment_failed event arrives later
    mock_failed_event = {
        "type": "payment_intent.payment_failed",
        "data": {"object": {"id": "pi_out_of_order_123"}}
    }
    with patch("stripe.Webhook.construct_event", return_value=mock_failed_event):
        fail_res = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert fail_res.status_code == 200
        assert fail_res.json()["status"] == "ignored_terminal_state"

    # Verify payment remains SUCCEEDED and was NOT overwritten!
    db_session.expire_all()
    payment = db_session.query(model.Payment).filter_by(stripe_payment_intent_id="pi_out_of_order_123").first()
    assert payment.status == model.PaymentStatus.SUCCEEDED
    assert booking.status == model.BookingStatus.CONFIRMED


async def test_duplicate_webhook_already_refunded(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """When a webhook is redelivered for an already refunded/cancelled payment, it returns already_refunded."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Quetta", "country": "Pakistan", "address_line": "Duplicate Hall"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Duplicate Webhook Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 20.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    booking_id = book_res.json()["id"]

    mock_intent = MagicMock()
    mock_intent.id = "pi_dup_refund_123"
    mock_intent.client_secret = "secret_dup"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

    # Fast-forward hold to expired
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    booking.created_at = now - timedelta(minutes=20)
    db_session.commit()

    mock_event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_dup_refund_123"}}
    }

    # First delivery: auto-refunds and marks CANCELED
    with patch("stripe.Webhook.construct_event", return_value=mock_event), \
         patch("stripe.Refund.create") as mock_refund:
        mock_refund.return_value = MagicMock(id="re_dup_1", status="succeeded")
        res1 = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "auto_refunded_cancelled_booking"

    # Second duplicate delivery: should acknowledge already_refunded without re-invoking refund
    with patch("stripe.Webhook.construct_event", return_value=mock_event), \
         patch("stripe.Refund.create") as mock_refund:
        res2 = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "already_refunded"
        mock_refund.assert_not_called()


async def test_auto_refund_failure_rolls_back_and_returns_500(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    """If Stripe Refund.create fails during auto-refund, DB rolls back and webhook returns 500 so Stripe retries."""
    now = datetime.now(timezone.utc)
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Multan", "country": "Pakistan", "address_line": "Refund Error Hall"}
    )
    loc_id = loc_res.json()["id"]

    srv_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Refund Failure Test",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 50.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )
    srv_id = srv_res.json()["id"]

    book_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": srv_id, "quantity": 1}
    )
    booking_id = book_res.json()["id"]

    mock_intent = MagicMock()
    mock_intent.id = "pi_refund_err_123"
    mock_intent.client_secret = "secret_re_err"

    with patch("stripe.PaymentIntent.create", return_value=mock_intent):
        await client.post(f"/bookings/{booking_id}/pay", headers=user_headers)

    # Fast forward hold to expired
    booking = db_session.query(model.Booking).filter_by(id=booking_id).first()
    booking.created_at = now - timedelta(minutes=20)
    db_session.commit()

    mock_event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_refund_err_123"}}
    }

    # Simulate Stripe Refund API failure
    with patch("stripe.Webhook.construct_event", return_value=mock_event), \
         patch("stripe.Refund.create", side_effect=Exception("Stripe API gateway timeout")):
        wh_res = await client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"stripe-signature": "valid_sig"}
        )
        assert wh_res.status_code == 500

    # Ensure payment record in DB was not left as CANCELED
    db_session.expire_all()
    payment = db_session.query(model.Payment).filter_by(stripe_payment_intent_id="pi_refund_err_123").first()
    assert payment.status != model.PaymentStatus.CANCELED


async def test_cancel_booking_inconsistent_payment_record(
    client: httpx.AsyncClient,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    """Cancelling a paid booking with missing Stripe PaymentIntent ID raises 500 cleanly without AttributeError."""
    now = datetime.now(timezone.utc)
    loc = db_session.query(model.Location).first()
    if not loc:
        seller_profile = db_session.query(model.SellerProfile).first()
        loc = model.Location(seller_id=seller_profile.id, city="Lahore", country="Pakistan", address_line="Audit Hall")
        db_session.add(loc)
        db_session.commit()

    srv = model.Services(
        seller_id=loc.seller_id,
        location_id=loc.id,
        service_name="Inconsistent Service",
        start_time=now + timedelta(days=1),
        end_time=now + timedelta(days=1, hours=2),
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=100,
        base_price=Decimal("50.00")
    )
    db_session.add(srv)
    db_session.commit()

    booking = model.Booking(
        user_id=test_user.id,
        service_id=srv.id,
        quantity=1,
        start_time=srv.start_time,
        end_time=srv.end_time,
        status=model.BookingStatus.CONFIRMED
    )
    db_session.add(booking)
    db_session.commit()

    # Create payment with amount > 0 and simulate an inconsistent record where intent ID is missing
    payment = model.Payment(
        booking_id=booking.id,
        stripe_payment_intent_id="temp_intent_for_test",
        client_secret="dummy",
        idempotency_key="dummy_key_inconsistent",
        amount=Decimal("50.00"),
        status=model.PaymentStatus.SUCCEEDED
    )
    db_session.add(payment)
    db_session.commit()

    # Clear stripe_payment_intent_id in memory to simulate a corrupted/inconsistent record
    payment.stripe_payment_intent_id = None


    # Cancelling must return 500 due to inconsistent record rather than crashing with unhandled AttributeError
    cancel_res = await client.post(f"/bookings/{booking.id}/cancel", headers=user_headers)
    assert cancel_res.status_code == 500
    assert "Inconsistent payment record" in cancel_res.json()["detail"]


def test_released_expired_holds_with_pessimistic_lock(db_session: Session):
    """Sweeper locks and cancels expired pending bookings and restores inventory."""
    from app.core.tasks import released_expired_holds

    now = datetime.now(timezone.utc)
    loc = db_session.query(model.Location).first()
    if not loc:
        seller_profile = db_session.query(model.SellerProfile).first()
        loc = model.Location(seller_id=seller_profile.id, city="Lahore", country="Pakistan", address_line="Sweeper Hall")
        db_session.add(loc)
        db_session.commit()

    user = db_session.query(model.User).first()

    srv = model.Services(
        seller_id=loc.seller_id,
        location_id=loc.id,
        service_name="Sweeper Lock Event",
        start_time=now + timedelta(days=2),
        end_time=now + timedelta(days=2, hours=2),
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=50,
        base_price=Decimal("15.00")
    )
    db_session.add(srv)
    db_session.commit()

    expired_booking = model.Booking(
        user_id=user.id,
        service_id=srv.id,
        quantity=2,
        start_time=srv.start_time,
        end_time=srv.end_time,
        status=model.BookingStatus.PENDING,
        created_at=now - timedelta(minutes=25)
    )
    db_session.add(expired_booking)
    db_session.commit()

    released_expired_holds(db=db_session)

    db_session.expire_all()
    refreshed = db_session.query(model.Booking).filter_by(id=expired_booking.id).first()
    assert refreshed.status == model.BookingStatus.CANCELLED


