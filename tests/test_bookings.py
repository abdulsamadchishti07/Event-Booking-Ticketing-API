from _pytest import pytester_assertions
from datetime import datetime, timezone, timedelta
import httpx
import pytest

from app import model
from sqlalchemy.orm import Session


async def setup_test_event_with_seats(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    # 1
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={
            "city": "Islamabad",
            "country": "Pakistan",
            "address_line": "Jinnah Hall"   
        }
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]
    
    # 2
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Standup Comedy Special",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 25.00   
        }
    )
    assert service_res.status_code == 201
    service_id = service_res.json()["id"]

    # 3
    tier_res = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={
            "name": "Front Row",
            "price": 50.00
        }
    )
    assert tier_res.status_code == 201
    tier_id = tier_res.json()["id"]

    # 4
    inv_res = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={
            "tier_id": tier_id,
            "identifier_codes": ["A-1", "A-2"]
        }
    )
    assert inv_res.status_code == 201
    
    return service_id, tier_id

# 1 Customer creates a 10-minute hold on a seat
async def test_create_booking_unit_assigned_success(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    db_session: Session 
):
    service_id, tier_id = await setup_test_event_with_seats(client, seller_headers)

    # Fetch seats A-1 from db
    seats = db_session.query(model.InventoryItems).filter_by(
        service_id=service_id,
        identifier_codes="A-1"
    ).first()

    now = datetime.now(timezone.utc)
    payload={
        "services_id": service_id,
        "tier_id": tier_id,
        "assigned_units_ids": [seats.id],
        "quantity": 1,
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat()
    }

    response = await client.post(
        "/bookings",
        headers=seller_headers,
        json=payload
    )
    assert response.status_code == 201
    data = response.json()["id"]

    assert data["status"] == "pending"
    assert data["quantity"] == 1
    assert "expires_at" in data

    # verify it setats status changed to RESERVED 
    db_session.refresh(seats)
    assert seats.status == model.ItemStatus.RESERVED

# 2nd customer try to reserved the same seats get error of 409
async def test_booking_already_reserved_seat_conflict(
   client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    db_session: Session
):
    service_id, tier_id = await setup_test_event_with_seats(client, seller_headers)

    # Fetch seats A-1 from db
    seats = db_session.query(model.InventoryItems).filter_by(
        service_id=service_id,
        identifier_codes="A-1"
    ).first()

    now = datetime.now(timezone.utc)
    payload={
        "services_id": service_id,
        "tier_id": tier_id,
        "assigned_units_ids": [seats.id],
        "quantity": 1,
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat()
    }

    # User 1 books seat A-1 -> 201 Created
    res1 = await client.post("/bookings", headers=seller_headers, json=payload)
    assert res1.status_code == 201

    # User 2 tries to book the same seat A-1 -> 409 Conflict!
    res2 = await client.post("/bookings", headers=seller_headers, json=payload)
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
    # No authorization header
    res = await client.post("/bookings", json=payload)
    assert res.status_code == 401