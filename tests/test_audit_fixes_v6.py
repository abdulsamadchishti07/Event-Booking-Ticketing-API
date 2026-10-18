from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import httpx
import pytest
from sqlalchemy.orm import Session
import sqlalchemy.exc

from app.core import oauth2, utils
from app.database import model, SessionLocal
from app.main import app


# ============================================================================
# 1. Price Snapshot Honored in /pay (booking.total_amount)
# ============================================================================

async def test_pay_uses_booking_total_amount_snapshot(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    db_session: Session
):
    """
    Verifies that when a booking has a locked total_amount snapshot ($20),
    and the seller subsequently raises the service price ($200),
    the checkout/payment intent uses the snapshot ($20 = 2000 cents).
    """
    # 1. Create a location and service at $10.00 base price
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Karachi", "country": "PK", "address_line": "Shahrah-e-Faisal"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Price Snapshot Concert",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 100,
            "base_price": 10.00,
            "start_time": (now + timedelta(days=3)).isoformat(),
            "end_time": (now + timedelta(days=3, hours=3)).isoformat()
        }
    )
    assert svc_res.status_code == 201
    svc_id = svc_res.json()["id"]

    # 2. Customer books 2 tickets ($20.00 total)
    bk_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": svc_id, "quantity": 2}
    )
    assert bk_res.status_code == 201
    booking_id = bk_res.json()["id"]

    # Verify total_amount in DB is 20.00
    db_booking = db_session.query(model.Booking).filter(model.Booking.id == booking_id).first()
    assert db_booking is not None
    assert Decimal(str(db_booking.total_amount)) == Decimal("20.00")

    # 3. Seller raises the price tenfold to $100.00
    upd_res = await client.put(
        f"/services/{svc_id}",
        headers=seller_headers,
        json={"base_price": 100.00}
    )
    assert upd_res.status_code == 200

    # 4. Initiate payment: mock Stripe PaymentIntent.create to verify amount_in_cents
    captured_amount = {}

    def mock_create(*args, **kwargs):
        captured_amount["amount"] = kwargs.get("amount")
        mock_obj = MagicMock()
        mock_obj.id = "pi_snapshot_test"
        mock_obj.client_secret = "pi_snapshot_secret"
        return mock_obj

    with patch("stripe.PaymentIntent.create", side_effect=mock_create):
        pay_res = await client.post(
            f"/bookings/{booking_id}/pay",
            headers=user_headers
        )
        assert pay_res.status_code == 200
        # Must be 2000 cents ($20.00), NOT 20000 cents ($200.00)
        assert captured_amount["amount"] == 2000


# ============================================================================
# 2. Soft-Deleting / Archiving Events Preserves Audit Trail
# ============================================================================

async def test_soft_delete_event_preserves_payment_and_cancellations(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    db_session: Session
):
    """
    Verifies that deleting an event with cancelled booking history does not
    wipe financial records (payments, cancellations). Instead, the event is
    archived (is_active=False) and hidden from public discovery and booking.
    """
    # 1. Create event
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "PK", "address_line": "Blue Area"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Audit Trail Festival",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 50.00,
            "start_time": (now + timedelta(days=4)).isoformat(),
            "end_time": (now + timedelta(days=4, hours=4)).isoformat()
        }
    )
    assert svc_res.status_code == 201
    svc_id = svc_res.json()["id"]

    # 2. Book and cancel
    bk_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": svc_id, "quantity": 1}
    )
    assert bk_res.status_code == 201
    booking_id = bk_res.json()["id"]

    # Create dummy payment record for audit trail verification
    payment = model.Payment(
        booking_id=booking_id,
        amount=Decimal("50.00"),
        currency="usd",
        stripe_payment_intent_id="pi_audit_test",
        client_secret="pi_sec_audit",
        idempotency_key="idem_audit_test",
        status=model.PaymentStatus.SUCCEEDED
    )
    db_session.add(payment)
    db_session.commit()

    cancel_res = await client.post(
        f"/bookings/{booking_id}/cancel",
        headers=user_headers,
        json={"reason": "Customer changed mind"}
    )
    assert cancel_res.status_code == 200

    # Verify cancellation row was created
    cancellation_count = db_session.query(model.Cancellation).filter(
        model.Cancellation.booking_id == booking_id
    ).count()
    assert cancellation_count >= 1

    # 3. Seller deletes the event (must archive with 204, not crash or 400)
    del_res = await client.delete(
        f"/services/{svc_id}",
        headers=seller_headers
    )
    assert del_res.status_code == 204

    # 4. Verify audit trail: payment and cancellation rows are NOT wiped
    payments_remaining = db_session.query(model.Payment).filter(
        model.Payment.booking_id == booking_id
    ).count()
    assert payments_remaining == 1, "Payment audit trail must be preserved!"

    cancellations_remaining = db_session.query(model.Cancellation).filter(
        model.Cancellation.booking_id == booking_id
    ).count()
    assert cancellations_remaining >= 1, "Cancellation audit trail must be preserved!"

    # 5. Verify the archived event is marked inactive
    svc_in_db = db_session.query(model.Services).filter(model.Services.id == svc_id).first()
    assert svc_in_db is not None
    assert svc_in_db.is_active is False

    # 6. Verify public endpoints hide or 404 on the archived event
    get_res = await client.get(f"/services/{svc_id}")
    assert get_res.status_code == 404

    search_res = await client.get("/services")
    assert search_res.status_code == 200
    returned_ids = [s["id"] for s in search_res.json()]
    assert svc_id not in returned_ids

    # 7. Attempting to book an archived event returns 404
    book_attempt = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": svc_id, "quantity": 1}
    )
    assert book_attempt.status_code == 404


async def test_hard_delete_event_with_zero_bookings(
    client: httpx.AsyncClient,
    seller_headers: dict,
    db_session: Session
):
    """
    Verifies that an event with zero booking history is cleanly hard-deleted.
    """
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Multan", "country": "PK", "address_line": "Cantt"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Zero Booking Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 30,
            "base_price": 15.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=2)).isoformat()
        }
    )
    assert svc_res.status_code == 201
    svc_id = svc_res.json()["id"]

    del_res = await client.delete(
        f"/services/{svc_id}",
        headers=seller_headers
    )
    assert del_res.status_code == 204

    svc_in_db = db_session.query(model.Services).filter(model.Services.id == svc_id).first()
    assert svc_in_db is None


# ============================================================================
# 3. User Deactivation & Real Password Scrambling
# ============================================================================

async def test_user_deactivation_scrambles_password_and_blocks_access(
    client: httpx.AsyncClient,
    seller_headers: dict,
    db_session: Session
):
    """
    Verifies that when a user with cancelled booking history deletes their account:
    - password_hash is scrambled with a secure random hash
    - is_active is set to False
    - is_verified is set to False
    - login with old credentials returns 403 Forbidden
    - access with existing token returns 401 Unauthorized
    """
    # 1. Register a new user
    ts = int(datetime.now().timestamp() * 1000)
    email = f"deactivate_user_{ts}@test.com"
    pwd = "SecurePassword123!"

    reg_res = await client.post(
        "/account/register",
        json={
            "name": "Deactivate Me",
            "email": email,
            "password": pwd,
            "dob": "1996-06-20",
            "phone_no": f"+155{ts % 100000000:08d}"
        }
    )
    assert reg_res.status_code == 201
    user_id = reg_res.json()["id"]

    # Verify user in DB and manually verify email for login
    db_user = db_session.query(model.User).filter(model.User.id == user_id).first()
    assert db_user is not None
    original_hash = db_user.password_hash
    db_user.is_verified = True
    db_session.commit()

    # Login to get token
    login_res = await client.post(
        "/login",
        data={"username": email, "password": pwd}
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    user_h = {"Authorization": f"Bearer {token}"}

    # 2. Create a service, book, and cancel so the user has cancelled bookings
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Quetta", "country": "PK", "address_line": "Jinnah Rd"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "User Deact Festival",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 20.00,
            "start_time": (now + timedelta(days=4)).isoformat(),
            "end_time": (now + timedelta(days=4, hours=4)).isoformat()
        }
    )
    assert svc_res.status_code == 201
    svc_id = svc_res.json()["id"]

    bk_res = await client.post(
        "/bookings",
        headers=user_h,
        json={"service_id": svc_id, "quantity": 1}
    )
    assert bk_res.status_code == 201
    booking_id = bk_res.json()["id"]

    cancel_res = await client.post(
        f"/bookings/{booking_id}/cancel",
        headers=user_h,
        json={"reason": "Testing cancellation"}
    )
    assert cancel_res.status_code == 200

    # 3. User deletes account (anonymization + deactivation with 204)
    del_acc_res = await client.delete(
        f"/account/{user_id}",
        headers=user_h
    )
    assert del_acc_res.status_code == 204

    # 4. Inspect DB state
    db_session.refresh(db_user)
    assert db_user.is_active is False, "is_active must be False"
    assert db_user.is_verified is False, "is_verified must be False"
    assert db_user.password_hash != original_hash, "password_hash must be overwritten"
    assert utils.verify_password(pwd, db_user.password_hash) is False, "Old password must not verify"

    # 5. Login attempt must fail with 403 Forbidden
    bad_login = await client.post(
        "/login",
        data={"username": email, "password": pwd}
    )
    assert bad_login.status_code in (401, 403)

    # 6. Authenticated call with prior token must return 401
    me_res = await client.get(
        "/account/me",
        headers=user_h
    )
    assert me_res.status_code == 401


# ============================================================================
# 4. Connection Pool Timeout Returns HTTP 503
# ============================================================================

async def test_pool_timeout_returns_http_503(client: httpx.AsyncClient):
    """
    Verifies that when SQLAlchemy raises TimeoutError on pool checkout,
    the API returns HTTP 503 Service Unavailable with Retry-After: 2.
    """
    # Temporarily mount a test endpoint that triggers TimeoutError
    @app.get("/test-pool-timeout-endpoint")
    def _trigger_timeout():
        raise sqlalchemy.exc.TimeoutError("QueuePool limit exceeded")

    try:
        res = await client.get("/test-pool-timeout-endpoint")
        assert res.status_code == 503
        assert res.headers.get("retry-after") == "2"
        assert "pool exhausted" in res.json()["detail"].lower()
    finally:
        # Clean up route from app
        app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/test-pool-timeout-endpoint"]
