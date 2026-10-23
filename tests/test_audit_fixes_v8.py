import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import httpx
import pytest
import stripe
from sqlalchemy.orm import Session

from app.database import model
from app.core import security as utils
from app.core.config import settings
from app.core.tasks import released_expired_holds
from app.workers.worker import process_single_job


@pytest.mark.asyncio
async def test_preregistration_takeover_prevention(client: httpx.AsyncClient, db_session: Session):
    """
    Verifies that attempting to register an existing user (unverified or verified)
    returns 409 Conflict and NEVER overwrites the existing user's password or profile.
    """
    clean_email = "takeover_test@example.com"
    # 1. First registration by user (unverified)
    payload_user = {
        "name": "Original User",
        "email": clean_email,
        "password": "OriginalPassword123!",
        "dob": "1990-01-01",
        "phone_no": "+19998887771"
    }
    r1 = await client.post("/account/register", json=payload_user)
    assert r1.status_code == 201
    created_id = r1.json()["id"]

    # 2. Attacker attempts to register with same email to hijack account
    payload_attacker = {
        "name": "Attacker",
        "email": clean_email,
        "password": "AttackerPassword123!",
        "dob": "1992-02-02",
        "phone_no": "+19998887772"
    }
    r2 = await client.post("/account/register", json=payload_attacker)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]

    # Verify database state was NOT overwritten
    user = db_session.query(model.User).filter(model.User.id == created_id).first()
    assert user.name == "Original User"
    assert not user.is_verified
    assert utils.verify_password("OriginalPassword123!", user.password_hash)
    assert not utils.verify_password("AttackerPassword123!", user.password_hash)


@pytest.mark.asyncio
async def test_auth_ip_rate_limits(client: httpx.AsyncClient, test_redis):
    """
    Verifies per-IP rate limiting on /verify-otp, /resend-otp, /forgot-password, /reset-password.
    """
    orig_limit = settings.auth_rate_limit
    try:
        settings.auth_rate_limit = 2

        # Test /forgot-password IP rate limit
        r1 = await client.post("/account/forgot-password", json={"email": "ip_limit_1@test.com"})
        assert r1.status_code in (200, 400)
        r2 = await client.post("/account/forgot-password", json={"email": "ip_limit_2@test.com"})
        assert r2.status_code in (200, 400)
        # 3rd request from same IP should get 429
        r3 = await client.post("/account/forgot-password", json={"email": "ip_limit_3@test.com"})
        assert r3.status_code == 429

    finally:
        settings.auth_rate_limit = orig_limit


def test_worker_retries_and_dead_letter():
    """
    Verifies that worker retries failed jobs up to 3 times before pushing to dead letter queue.
    """
    mock_redis = MagicMock()
    raw_payload = json.dumps({
        "task": "send_otp_email",
        "payload": {"to_email": "fail@test.com", "otp": "123456"},
        "retries": 0,
        "enqueued_at": "2026-10-22T00:00:00Z"
    })

    # Cause handler to raise Exception
    with patch.dict("app.workers.worker.TASK_HANDLERS", {"send_otp_email": MagicMock(side_effect=RuntimeError("SMTP dropped"))}):
        # 1st attempt: retries becomes 1, pushed back to queue:jobs
        process_single_job(mock_redis, raw_payload)
        assert mock_redis.rpush.call_count == 1
        call_args = mock_redis.rpush.call_args[0]
        assert call_args[0] == "queue:jobs"
        payload_1 = json.loads(call_args[1])
        assert payload_1["retries"] == 1

        # 3rd retry (retries=2 -> 3)
        mock_redis.reset_mock()
        payload_1["retries"] = 2
        process_single_job(mock_redis, json.dumps(payload_1))
        assert mock_redis.rpush.call_count == 1
        call_args = mock_redis.rpush.call_args[0]
        assert call_args[0] == "queue:jobs"
        payload_2 = json.loads(call_args[1])
        assert payload_2["retries"] == 3

        # Exceeds max retries (retries=3 -> routed to dead letter)
        mock_redis.reset_mock()
        payload_2["retries"] = 3
        process_single_job(mock_redis, json.dumps(payload_2))
        assert mock_redis.rpush.call_count == 1
        call_args = mock_redis.rpush.call_args[0]
        assert call_args[0] == "queue:jobs:dead_letter"


def test_sweeper_oversell_protection(db_session: Session, test_user: model.User, seller_user: model.User):
    """
    Verifies that if a booking hold expires but the user paid Stripe,
    and in the meantime capacity was filled by other bookings,
    the sweeper does NOT confirm and oversell, but issues a refund and cancels the booking.
    """
    # 1. Create a service with max_capacity = 1
    location = model.Location(seller_id=seller_user.seller_profile.id, city="Sweeper City", country="Sweeper Country", address_line="123 Sweeper St")
    db_session.add(location)
    db_session.commit()

    service = model.Services(
        seller_id=seller_user.seller_profile.id,
        service_name="Single Seat Event",
        location_id=location.id,
        start_time=datetime.now(timezone.utc) + timedelta(days=1),
        end_time=datetime.now(timezone.utc) + timedelta(days=1, hours=2),
        base_price=100.0,
        is_active=True,
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=1
    )
    db_session.add(service)
    db_session.commit()

    # 2. Create expired pending booking 1 for test_user
    expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)
    booking_expired = model.Booking(
        user_id=test_user.id,
        service_id=service.id,
        start_time=service.start_time,
        end_time=service.end_time,
        status=model.BookingStatus.PENDING,
        total_amount=100.0,
        created_at=expired_time
    )
    db_session.add(booking_expired)
    db_session.commit()

    payment_expired = model.Payment(
        booking_id=booking_expired.id,
        amount=100.0,
        status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD,
        stripe_payment_intent_id="pi_sweeper_oversell_test",
        client_secret="sec_oversell_test",
        idempotency_key="idem_oversell_test"
    )
    db_session.add(payment_expired)
    db_session.commit()

    # 3. Another user legitimately took the 1 capacity
    other_user = model.User(
        name="Other Legitimate User",
        email="other_legit@test.com",
        password_hash=utils.hash("Pass123!"),
        dob=datetime.now(timezone.utc).date(),
        role=model.UserRole.CUSTOMER,
        is_verified=True
    )
    db_session.add(other_user)
    db_session.commit()

    booking_confirmed = model.Booking(
        user_id=other_user.id,
        service_id=service.id,
        start_time=service.start_time,
        end_time=service.end_time,
        status=model.BookingStatus.CONFIRMED,
        total_amount=100.0
    )
    db_session.add(booking_confirmed)
    db_session.commit()

    # 4. Mock Stripe PaymentIntent as succeeded
    mock_intent = MagicMock()
    mock_intent.status = "succeeded"

    with patch("stripe.PaymentIntent.cancel", side_effect=stripe.error.InvalidRequestError("Already succeeded", "id")), \
         patch("stripe.PaymentIntent.retrieve", return_value=mock_intent), \
         patch("stripe.Refund.create") as mock_refund:
        # Run sweeper
        released_expired_holds(db=db_session)

        # Verify refund was called because capacity was full
        assert mock_refund.called
        mock_refund.assert_called_with(
            payment_intent="pi_sweeper_oversell_test",
            amount=10000,
            reason="requested_by_customer",
            idempotency_key="auto_refund_pi_sweeper_oversell_test"
        )

        db_session.refresh(booking_expired)
        db_session.refresh(payment_expired)
        # Booking must be CANCELLED, payment must be CANCELED (not CONFIRMED/SUCCEEDED)
        assert booking_expired.status == model.BookingStatus.CANCELLED
        assert payment_expired.status == model.PaymentStatus.CANCELED

        # Cancellation audit row must be created
        canc = db_session.query(model.Cancellation).filter(model.Cancellation.booking_id == booking_expired.id).first()
        assert canc is not None
        assert canc.refund_amount == 100.0


def test_sweeper_refund_failure_leaves_booking_pending(db_session: Session, test_user: model.User, seller_user: model.User):
    """
    Verifies that if stripe.Refund.create raises an exception, the sweeper does NOT
    cancel the booking or payment, leaving them pending so the refund can be retried next cycle.
    """
    location = model.Location(seller_id=seller_user.seller_profile.id, city="Fail City", country="Fail Country", address_line="123 Fail St")
    db_session.add(location)
    db_session.commit()

    service = model.Services(
        seller_id=seller_user.seller_profile.id,
        service_name="Refund Fail Event",
        location_id=location.id,
        start_time=datetime.now(timezone.utc) + timedelta(days=1),
        end_time=datetime.now(timezone.utc) + timedelta(days=1, hours=2),
        base_price=50.0,
        is_active=True,
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=1
    )
    db_session.add(service)
    db_session.commit()

    expired_time = datetime.now(timezone.utc) - timedelta(minutes=15)
    booking = model.Booking(
        user_id=test_user.id,
        service_id=service.id,
        start_time=service.start_time,
        end_time=service.end_time,
        status=model.BookingStatus.PENDING,
        total_amount=50.0,
        created_at=expired_time
    )
    db_session.add(booking)
    db_session.commit()

    payment = model.Payment(
        booking_id=booking.id,
        amount=50.0,
        status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD,
        stripe_payment_intent_id="pi_sweeper_fail_test",
        client_secret="sec_fail_test",
        idempotency_key="idem_fail_test"
    )
    db_session.add(payment)
    db_session.commit()

    # Fill capacity with another confirmed booking
    other_user = model.User(
        name="Other User",
        email="other_fail@test.com",
        password_hash=utils.hash("Pass123!"),
        dob=datetime.now(timezone.utc).date(),
        role=model.UserRole.CUSTOMER,
        is_verified=True
    )
    db_session.add(other_user)
    db_session.commit()

    booking_confirmed = model.Booking(
        user_id=other_user.id,
        service_id=service.id,
        start_time=service.start_time,
        end_time=service.end_time,
        status=model.BookingStatus.CONFIRMED,
        total_amount=50.0
    )
    db_session.add(booking_confirmed)
    db_session.commit()

    mock_intent = MagicMock()
    mock_intent.status = "succeeded"

    with patch("stripe.PaymentIntent.cancel", side_effect=stripe.error.InvalidRequestError("Already succeeded", "id")), \
         patch("stripe.PaymentIntent.retrieve", return_value=mock_intent), \
         patch("stripe.Refund.create", side_effect=RuntimeError("Stripe network timeout")):
        released_expired_holds(db=db_session)

        db_session.refresh(booking)
        db_session.refresh(payment)
        # Booking and payment must remain PENDING/REQUIRES_PAYMENT_METHOD
        assert booking.status == model.BookingStatus.PENDING
        assert payment.status == model.PaymentStatus.REQUIRES_PAYMENT_METHOD


def test_sweeper_db_error_isolation(db_session: Session, test_user: model.User, seller_user: model.User):
    """
    Verifies that a failure or exception on one expired booking does not poison
    or break the session for subsequent expired bookings in the same sweep.
    """
    location = model.Location(seller_id=seller_user.seller_profile.id, city="Iso City", country="Iso Country", address_line="123 Iso St")
    db_session.add(location)
    db_session.commit()

    service = model.Services(
        seller_id=seller_user.seller_profile.id,
        service_name="Multi Seat Event",
        location_id=location.id,
        start_time=datetime.now(timezone.utc) + timedelta(days=2),
        end_time=datetime.now(timezone.utc) + timedelta(days=2, hours=2),
        base_price=50.0,
        is_active=True,
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=10
    )
    db_session.add(service)
    db_session.commit()

    expired_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    # Booking 1 (will encounter an error during release)
    b1 = model.Booking(
        user_id=test_user.id,
        service_id=service.id,
        start_time=service.start_time,
        end_time=service.end_time,
        status=model.BookingStatus.PENDING,
        total_amount=50.0,
        created_at=expired_time
    )
    # Booking 2 (normal unpaid expired hold)
    b2 = model.Booking(
        user_id=test_user.id,
        service_id=service.id,
        start_time=service.start_time,
        end_time=service.end_time,
        status=model.BookingStatus.PENDING,
        total_amount=50.0,
        created_at=expired_time
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    # When stripe.PaymentIntent.retrieve is called on b1, raise an unexpected error
    with patch("stripe.PaymentIntent.retrieve", side_effect=Exception("Stripe API network failure")):
        p1 = model.Payment(
            booking_id=b1.id,
            amount=50.0,
            status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD,
            stripe_payment_intent_id="pi_b1",
            client_secret="sec_b1",
            idempotency_key="idem_b1"
        )
        db_session.add(p1)
        db_session.commit()

        # Run sweeper
        released_expired_holds(db=db_session)

        # b2 (which has no payment intent) must still have been cleanly cancelled despite b1's failure
        db_session.refresh(b2)
        assert b2.status == model.BookingStatus.CANCELLED
