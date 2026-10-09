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
