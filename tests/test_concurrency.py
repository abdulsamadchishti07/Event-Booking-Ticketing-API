import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import httpx
import pytest

from app import oauth2, utils
from app.database import get_db, SessionLocal, model
from app.main import app


def _create_committed_unit_event():
    with SessionLocal() as db:
        ts = int(datetime.now().timestamp() * 1000)
        user = model.User(
            name="Concurrent Customer",
            email=f"concurrent_cust_{ts}@test.com",
            password_hash=utils.hash("Password123!"),
            dob=date(1995, 1, 1),
            role=model.UserRole.CUSTOMER,
            phone_no=f"+199{ts % 100000000:08d}",
            is_verified=True,
        )
        seller_u = model.User(
            name="Concurrent Seller",
            email=f"concurrent_seller_{ts}@test.com",
            password_hash=utils.hash("Password123!"),
            dob=date(1990, 1, 1),
            role=model.UserRole.SELLER,
            phone_no=f"+188{ts % 100000000:08d}",
            is_verified=True,
        )
        db.add_all([user, seller_u])
        db.commit()
        db.refresh(user)
        db.refresh(seller_u)

        seller_profile = model.SellerProfile(
            user_id=seller_u.id,
            business_name="Concurrency Unit Arena Org",
            is_verified=True
        )
        db.add(seller_profile)
        db.commit()
        db.refresh(seller_profile)

        loc = model.Location(
            seller_id=seller_profile.id,
            city="Concurrency City",
            country="PK",
            address_line="123 Unit Arena St"
        )
        db.add(loc)
        db.commit()
        db.refresh(loc)

        now = datetime.now(timezone.utc)
        srv = model.Services(
            seller_id=seller_profile.id,
            location_id=loc.id,
            service_name="Concurrency Unit Arena",
            booking_mode=model.BookingMode.UNIT_ASSIGNED,
            base_price=Decimal("50.00"),
            start_time=now + timedelta(days=2),
            end_time=now + timedelta(days=2, hours=3)
        )
        db.add(srv)
        db.commit()
        db.refresh(srv)

        tier = model.ServiceTier(
            service_id=srv.id,
            name="VIP",
            price=Decimal("50.00")
        )
        db.add(tier)
        db.commit()
        db.refresh(tier)

        seat = model.InventoryItems(
            service_id=srv.id,
            tier_id=tier.id,
            identifier_code="CONC-A1",
            status=model.ItemStatus.AVAILABLE
        )
        db.add(seat)
        db.commit()
        db.refresh(seat)

        user_token = oauth2.create_access_token({"user_id": user.id, "sub": str(user.id)})
        user_headers = {"Authorization": f"Bearer {user_token}"}

        return {
            "user_id": user.id,
            "seller_user_id": seller_u.id,
            "service_id": srv.id,
            "tier_id": tier.id,
            "seat_id": seat.id,
            "user_headers": user_headers
        }


def _create_committed_slot_event():
    with SessionLocal() as db:
        ts = int(datetime.now().timestamp() * 1000) + 1
        user = model.User(
            name="Concurrent Slot Customer",
            email=f"concurrent_slot_cust_{ts}@test.com",
            password_hash=utils.hash("Password123!"),
            dob=date(1995, 1, 1),
            role=model.UserRole.CUSTOMER,
            phone_no=f"+177{ts % 100000000:08d}",
            is_verified=True,
        )
        seller_u = model.User(
            name="Concurrent Slot Seller",
            email=f"concurrent_slot_seller_{ts}@test.com",
            password_hash=utils.hash("Password123!"),
            dob=date(1990, 1, 1),
            role=model.UserRole.SELLER,
            phone_no=f"+166{ts % 100000000:08d}",
            is_verified=True,
        )
        db.add_all([user, seller_u])
        db.commit()
        db.refresh(user)
        db.refresh(seller_u)

        seller_profile = model.SellerProfile(
            user_id=seller_u.id,
            business_name="Concurrency Slot Arena Org",
            is_verified=True
        )
        db.add(seller_profile)
        db.commit()
        db.refresh(seller_profile)

        loc = model.Location(
            seller_id=seller_profile.id,
            city="Concurrency City",
            country="PK",
            address_line="123 Slot Arena St"
        )
        db.add(loc)
        db.commit()
        db.refresh(loc)

        now = datetime.now(timezone.utc)
        srv = model.Services(
            seller_id=seller_profile.id,
            location_id=loc.id,
            service_name="Concurrency Slot Arena",
            booking_mode=model.BookingMode.SLOT_CAPACITY,
            max_capacity=1,
            base_price=Decimal("25.00"),
            start_time=now + timedelta(days=2),
            end_time=now + timedelta(days=2, hours=3)
        )
        db.add(srv)
        db.commit()
        db.refresh(srv)

        user_token = oauth2.create_access_token({"user_id": user.id, "sub": str(user.id)})
        user_headers = {"Authorization": f"Bearer {user_token}"}

        return {
            "user_id": user.id,
            "seller_user_id": seller_u.id,
            "service_id": srv.id,
            "user_headers": user_headers
        }


def _cleanup_committed_event(ids: dict):
    with SessionLocal() as db:
        # Delete booking seat assignments and payments
        bookings = db.query(model.Booking).filter(model.Booking.service_id == ids["service_id"]).all()
        for b in bookings:
            db.execute(model.booking_units.delete().where(model.booking_units.c.booking_id == b.id))
            db.query(model.Payment).filter_by(booking_id=b.id).delete()
            db.delete(b)
        db.commit()

        srv = db.query(model.Services).filter_by(id=ids["service_id"]).first()
        if srv:
            db.query(model.InventoryItems).filter_by(service_id=srv.id).delete()
            db.query(model.ServiceTier).filter_by(service_id=srv.id).delete()
            db.delete(srv)
        db.commit()

        u1 = db.query(model.User).filter_by(id=ids["user_id"]).first()
        if u1:
            db.delete(u1)
        u2 = db.query(model.User).filter_by(id=ids["seller_user_id"]).first()
        if u2:
            db.delete(u2)
        db.commit()


async def test_high_concurrency_booking_race_condition(client: httpx.AsyncClient):
    """
    STRESS TEST: Fire 25 simultaneous booking requests for the exact same seat across separate DB sessions.
    Expected outcome: Exactly 1 succeeds (201). Exactly 24 fail with (409 Conflict).
    Uses committed data and separate DB sessions so Postgres row-level locks genuinely contend.
    """
    data = _create_committed_unit_event()
    service_id = data["service_id"]
    tier_id = data["tier_id"]
    seat_id = data["seat_id"]
    user_headers = data["user_headers"]

    now = datetime.now(timezone.utc)
    payload = {
        "service_id": service_id,
        "tier_id": tier_id,
        "assigned_unit_ids": [seat_id],
        "quantity": 1,
        "start_time": (now + timedelta(days=2)).isoformat(),
        "end_time": (now + timedelta(days=2, hours=2)).isoformat()
    }

    # Remove get_db override so every request opens a separate SessionLocal connection
    orig_override = app.dependency_overrides.pop(get_db, None)

    try:
        tasks = [client.post("/bookings", headers=user_headers, json=payload) for _ in range(25)]
        responses = await asyncio.gather(*tasks)

        status_codes = [res.status_code for res in responses]
        success_count = status_codes.count(201)
        conflict_count = status_codes.count(409)

        assert success_count == 1, f"Expected exactly 1 success, got {success_count}. Statuses: {status_codes}"
        assert conflict_count == 24, f"Expected exactly 24 conflicts, got {conflict_count}. Statuses: {status_codes}"

        with SessionLocal() as check_session:
            seat_in_db = check_session.query(model.InventoryItems).filter_by(id=seat_id).first()
            assert seat_in_db.status == model.ItemStatus.RESERVED
    finally:
        if orig_override is not None:
            app.dependency_overrides[get_db] = orig_override
        _cleanup_committed_event(data)


async def test_slot_capacity_concurrency_race_condition(client: httpx.AsyncClient):
    """
    STRESS TEST: Fire 25 simultaneous booking requests for an event with capacity=1 across separate DB sessions.
    Expected outcome: Exactly 1 succeeds (201). Exactly 24 fail with (409 Conflict - Sold out).
    Uses committed data and separate DB sessions so Postgres row-level locks genuinely contend.
    """
    data = _create_committed_slot_event()
    service_id = data["service_id"]
    user_headers = data["user_headers"]

    payload = {
        "service_id": service_id,
        "quantity": 1
    }

    orig_override = app.dependency_overrides.pop(get_db, None)

    try:
        tasks = [client.post("/bookings", headers=user_headers, json=payload) for _ in range(25)]
        responses = await asyncio.gather(*tasks)

        status_codes = [res.status_code for res in responses]
        success_count = status_codes.count(201)
        conflict_count = status_codes.count(409)

        assert success_count == 1, f"Expected exactly 1 successful booking, got {success_count}. Statuses: {status_codes}"
        assert conflict_count == 24, f"Expected exactly 24 conflicts, got {conflict_count}. Statuses: {status_codes}"

        with SessionLocal() as check_session:
            total_booked = check_session.query(model.Booking).filter(
                model.Booking.service_id == service_id,
                model.Booking.status == model.BookingStatus.PENDING
            ).count()
            assert total_booked == 1
    finally:
        if orig_override is not None:
            app.dependency_overrides[get_db] = orig_override
        _cleanup_committed_event(data)


