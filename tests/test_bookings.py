from datetime import datetime, timedelta, timezone
import httpx
import pytest
from sqlalchemy.orm import Session
from app.core.tasks import released_expired_holds
from app.database import model


async def setup_test_event_with_seats(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    # 1. Location
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "Pakistan", "address_line": "Jinnah Hall"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]
    
    # 2. Service
    now = datetime.now(timezone.utc)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Standup Comedy Special",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 25.00,
            "start_time": (now + timedelta(days=1)).isoformat(),
            "end_time": (now + timedelta(days=1, hours=3)).isoformat()
        }
    )
    assert service_res.status_code == 201
    service_id = service_res.json()["id"]

    # 3. Tier
    tier_res = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "Front Row", "price": 50.00}
    )
    assert tier_res.status_code == 201
    tier_id = tier_res.json()["id"]

    # 4. Bulk Seats
    inv_res = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={"tier_id": tier_id, "identifier_codes": ["A-1", "A-2"]}
    )
    assert inv_res.status_code == 201
    
    return service_id, tier_id


# 1. Customer creates a 10-minute hold on a seat
async def test_create_booking_unit_assigned_success(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session 
):
    service_id, tier_id = await setup_test_event_with_seats(client, seller_headers)

    seat = db_session.query(model.InventoryItems).filter_by(
        service_id=service_id,
        identifier_code="A-1"
    ).first()

    now = datetime.now(timezone.utc)
    payload = {
        "service_id": service_id,
        "tier_id": tier_id,
        "assigned_unit_ids": [seat.id],
        "quantity": 1,
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat()
    }

    response = await client.post("/bookings", headers=user_headers, json=payload)
    assert response.status_code == 201
    data = response.json()

    assert data["status"] == "pending"
    assert data["quantity"] == 1
    #assert "expires_at" in data

    db_session.refresh(seat)
    assert seat.status == model.ItemStatus.RESERVED


# 2. Second customer trying to book the same reserved seat gets 409 Conflict
async def test_booking_already_reserved_seat_conflict(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    service_id, tier_id = await setup_test_event_with_seats(client, seller_headers)

    seat = db_session.query(model.InventoryItems).filter_by(
        service_id=service_id,
        identifier_code="A-1"
    ).first()

    now = datetime.now(timezone.utc)
    payload = {
        "service_id": service_id,
        "tier_id": tier_id,
        "assigned_unit_ids": [seat.id],
        "quantity": 1,
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat()
    }

    # User 1 books seat A-1 -> 201 Created
    res1 = await client.post("/bookings", headers=user_headers, json=payload)
    assert res1.status_code == 201

    # User 2 tries to book the same seat A-1 -> 409 Conflict!
    res2 = await client.post("/bookings", headers=user_headers, json=payload)
    assert res2.status_code == 409


# 3. Unauthenticated guest is rejected (401)
async def test_booking_unauthenticated_rejected(client: httpx.AsyncClient):
    now = datetime.now(timezone.utc)
    payload = {
        "service_id": 1,
        "quantity": 1,
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat(),
    }
    res = await client.post("/bookings", json=payload)
    assert res.status_code == 401


async def setup_test_event_slot_capacity(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    max_capacity: int = 5
):
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Lahore", "country": "Pakistan", "address_line": "Alhamra"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "General Admission Concert",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": max_capacity,
            "base_price": 10.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )
    assert service_res.status_code == 201
    return service_res.json()["id"]


# 4. Customer successfully books general admission
async def test_create_booking_slot_capacity_success(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):
    service_id = await setup_test_event_slot_capacity(client, seller_headers, max_capacity=10)

    now = datetime.now(timezone.utc)
    payload = {
        "service_id": service_id,
        "quantity": 3,
        "start_time": (now + timedelta(days=2)).isoformat(),
        "end_time": (now + timedelta(days=2, hours=3)).isoformat()
    }

    response = await client.post("/bookings", headers=user_headers, json=payload)
    assert response.status_code == 201
    data = response.json()

    assert data["status"] == "pending"
    assert data["quantity"] == 3
    assert "created_at" in data


# 5. Sold out → 409 Conflict when max_capacity exceeded
async def test_booking_slot_capacity_sold_out(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):
    service_id = await setup_test_event_slot_capacity(client, seller_headers, max_capacity=2)

    now = datetime.now(timezone.utc)
    payload = {
        "service_id": service_id,
        "quantity": 2,
        "start_time": (now + timedelta(days=2)).isoformat(),
        "end_time": (now + timedelta(days=2, hours=3)).isoformat()
    }

    # Takes all 2 spots → 201
    res1 = await client.post("/bookings", headers=user_headers, json=payload)
    assert res1.status_code == 201

    # Tries 1 more but 0 remain → 409
    payload["quantity"] = 1
    res2 = await client.post("/bookings", headers=user_headers, json=payload)
    assert res2.status_code == 409
    assert "Sold out" in res2.json()["detail"]

# 6. Sweeper successfully cleans up expired holds
async def test_sweeper_cancels_expired_holds(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session: Session
):
    # 1
    # Setup event and book a seat
    service_id, tier_id = await setup_test_event_with_seats(client, seller_headers)
    seat = db_session.query(model.InventoryItems).filter_by(
        service_id=service_id, identifier_code="A-1"
    ).first()

    now = datetime.now(timezone.utc)
    payload = {
        "service_id": service_id,
        "tier_id": tier_id,
        "assigned_unit_ids": [seat.id],
        "quantity": 1,
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat()
    }
    # User successfully books the seat
    res = await client.post("/bookings", headers=user_headers, json=payload)
    assert res.status_code == 201
    booking_id = res.json()["id"]

    # 2
    # HACK THE MATRIX: Fast forward time! 
    # We manually alter the database to pretend this booking happened 15 minutes ago.
    booking_record = db_session.get(model.Booking, booking_id)
    booking_record.created_at = now - timedelta(minutes=15)
    db_session.commit()

    # 3
    # Trigger the Sweeper manually
    released_expired_holds(db=db_session)

    # 4 
    # Verify the Sweeper did its job
    db_session.refresh(booking_record)
    db_session.refresh(seat)

    assert booking_record.status == model.BookingStatus.CANCELLED
    assert seat.status == model.ItemStatus.AVAILABLE


async def test_booking_slot_capacity_rejects_assigned_units(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):
    # Setup a slot_capacity event
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "Pakistan", "address_line": "F-9 Park"}
    )
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Open Air Festival",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "base_price": 20.00,
            "max_capacity": 100,
            "start_time": (now + timedelta(days=3)).isoformat(),
            "end_time": (now + timedelta(days=3, hours=4)).isoformat()
        }
    )
    svc_id = svc_res.json()["id"]

    tier_res = await client.post(
        f"/services/{svc_id}/tiers",
        headers=seller_headers,
        json={"name": "General Admission", "price": 20.00}
    )
    tier_id = tier_res.json()["id"]

    now = datetime.now(timezone.utc)
    # Attempt to pass assigned_unit_ids to a slot_capacity event
    payload = {
        "service_id": svc_id,
        "tier_id": tier_id,
        "quantity": 1,
        "assigned_unit_ids": [9999],
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat()
    }

    res = await client.post("/bookings", headers=user_headers, json=payload)
    assert res.status_code == 400
    assert "cannot be provided for slot_capacity" in res.json()["detail"]

