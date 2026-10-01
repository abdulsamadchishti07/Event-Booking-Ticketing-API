from datetime import datetime, timedelta, timezone
import httpx
import pytest

from app.database import model

def make_test_time_window(days_ahead=7, duration_hours=4):
    now = datetime.now(timezone.utc)
    return {
        "start_time": (now + timedelta(days=days_ahead)).isoformat(),
        "end_time": (now + timedelta(days=days_ahead, hours=duration_hours)).isoformat()
    }



async def create_test_location(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
) -> int:
    res = await client.post(
        "/locations",
        headers=seller_headers,
        json={
            "city": "Lahore", 
            "country": "Pakistan", 
            "address_line": "Expo Center Hall 1"
        }
    )
    assert res.status_code == 201
    return res.json()["id"]

# 1 Verified Seller creates a slot_capacity event (General)
async def test_create_service_slot_capacity(
    client: httpx.AsyncClient,
    seller_user: model.User,
    seller_headers: dict[str, str]
):
    location_id = await create_test_location(client, seller_headers)
    
    payload = {
        "service_name": "Tech Conference 2026",
        "service_desc": "Annual Developers Summit",
        "location_id": location_id,
        "booking_mode": "slot_capacity",
        "max_capacity": 500,
        "base_price": 50.00,
        **make_test_time_window()
    }

    responses = await client.post("/services", headers=seller_headers, json=payload) 
    assert responses.status_code == 201
    data = responses.json()

    assert data["service_name"] == "Tech Conference 2026"
    assert data["booking_mode"] == "slot_capacity"
    assert data["max_capacity"] == 500
    assert float(data["base_price"]) == 50.00
    assert data["seller_id"] == seller_user.seller_profile.id
    assert "id" in data

# 2 Verify Seller create the unit_assigned event (Number Seating)
async def test_create_service_unit_assigned(
    client: httpx.AsyncClient,
    seller_user: model.User,
    seller_headers: dict[str, str]
):
    location_id = await create_test_location(client, seller_headers)

    payload = {
        "service_name": "THE Avengers Doom Days",
        "service_desc": "Ticket of Movie The new so called avengergs getting betting by Dr.Victor Doom",
        "location_id": location_id,
        "booking_mode": "unit_assigned",
        "base_price": 100.00,
        **make_test_time_window()
    }

    response = await client.post(
        "/services",
        headers=seller_headers,
        json=payload
    )
    assert response.status_code == 201
    data = response.json()

    assert data["service_name"] == "THE Avengers Doom Days"
    assert data["booking_mode"] == "unit_assigned"
    assert data["seller_id"] == seller_user.seller_profile.id

# 3 User without seller acoount are not being able to create a service
async def test_create_service_customer_forbiden(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):
    location_id = await create_test_location(client, seller_headers)
    payload = {
        "service_name": "Unauthorized Event",
        "location_id": location_id,
        "booking_mode": "slot_capacity",
        "base_price": 10.00,
        **make_test_time_window()
    }
    responses = await client.post(
        "/services",
        headers=user_headers,
        json=payload
    )
    assert responses.status_code == 403

# 4 Service with non-existent location is rejected
async def test_create_service_invalid_location(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):

    payload = {
        "service_name": "Unauthorized Event",
        "location_id": 99999,
        "booking_mode": "slot_capacity",
        "base_price": 10.00,
        **make_test_time_window()
    }

    responses = await client.post(
        "/services",
        headers=seller_headers,
        json=payload
    )
    assert responses.status_code == 404
# 5. Verified Seller creates a pricing tier for their event
async def test_create_service_tier_success(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    # 1. Create location & service
    loc_id = await create_test_location(client, seller_headers)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Music Festival 2026",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 1000,
            "base_price": 40.00,
        **make_test_time_window()
        }
    )
    service_id = service_res.json()["id"]

    # 2. Add VIP Tier
    tier_payload = {
        "name": "VIP Pass",
        "price": 120.00
    }
    response = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json=tier_payload
    )
    assert response.status_code == 201
    data = response.json()

    assert data["name"] == "VIP Pass"
    assert float(data["price"]) == 120.00
    assert data["service_id"] == service_id
    assert "id" in data


# 6. Duplicate tier name on the same event returns 409 Conflict
async def test_create_duplicate_tier_conflict(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    loc_id = await create_test_location(client, seller_headers)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Comedy Night",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 200,
            "base_price": 20.00,
        **make_test_time_window()
        }
    )
    service_id = service_res.json()["id"]

    # Create first VIP tier
    await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "VIP", "price": 50.00}
    )

    # Try creating second VIP tier with the same name -> 409 Conflict
    res_dup = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "VIP", "price": 60.00}
    )
    assert res_dup.status_code == 409


# 7. Customer is forbidden from creating tiers
async def test_create_tier_customer_forbidden(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):
    loc_id = await create_test_location(client, seller_headers)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Art Exhibition",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 300,
            "base_price": 15.00,
        **make_test_time_window()
        }
    )
    service_id = service_res.json()["id"]

    response = await client.post(
        f"/services/{service_id}/tiers",
        headers=user_headers,
        json={"name": "Gold", "price": 30.00}
    )
    assert response.status_code == 403

# 8 Verifiied Seller bulk generate seats for a unit_assigned
async def test_bulk_inventory_generator_success(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    loc_id = await create_test_location(client, seller_headers)
    # Create venue, unit_assigned event, and a tier
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Cinema Premier",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 15.00,
        **make_test_time_window()
        }
    )

    service_id = service_res.json()["id"]

    tier_res = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "Standard", "price": 15.00}
    )

    tier_id = tier_res.json()["id"]

    # Buld Genereate Seates
    payload = {
        "tier_id": tier_id,
        "identifier_codes": ["A-1", "A-2", "A-3", "A-4", "A-5"]
    }

    responses = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json=payload
    )

    assert responses.status_code == 201
    data = responses.json()
    assert data["created_count"] == 5

# 9. Bulk seat generation rejected for slot_capacity events
async def test_bulk_inventory_slot_capacity_rejected(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    loc_id = await create_test_location(client, seller_headers)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Standing Concert",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 500,
            "base_price": 25.00,
        **make_test_time_window()
        }
    )

    service_id = service_res.json()["id"]

    tier_res= await client.post(
        f"services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "General", "price": 25.00}
    )
    tier_id = tier_res.json()["id"]

    responses = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={"tier_id": tier_id, "identifier_codes": ["Seat-1"]}
    )

    assert responses.status_code == 400

async def test_bulk_inventory_duplicate_seat_conflict(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
):
    loc_id = await create_test_location(client, seller_headers)

    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Theater Show",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 50.00,
        **make_test_time_window(),
        },
    )
    assert service_res.status_code == 201
    service_id = service_res.json()["id"]

    tier_res = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={
            "name": "Balcony",
            "price": 50.00,
        },
    )
    assert tier_res.status_code == 201
    tier_id = tier_res.json()["id"]

    # Create B-1 first.
    existing_res = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={
            "tier_id": tier_id,
            "identifier_codes": ["B-1"],
        },
    )
    assert existing_res.status_code == 201

    # B-1 already exists, so the bulk request should conflict.
    res_dup = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={
            "tier_id": tier_id,
            "identifier_codes": ["B-1", "B-2"],
        },
    )

    assert res_dup.status_code == 409


# 11. Duplicate identifier codes within the same request are caught cleanly (422) instead of hitting 500 IntegrityError
async def test_bulk_inventory_duplicate_codes_in_request_rejected(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
):
    loc_id = await create_test_location(client, seller_headers)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Orchestra Night",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 75.00,
        **make_test_time_window(),
        },
    )
    service_id = service_res.json()["id"]

    tier_res = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "Front Row", "price": 75.00},
    )
    tier_id = tier_res.json()["id"]

    # Request containing duplicate code within the same payload: ["C-1", "C-2", "C-1"]
    res_dup_in_req = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={
            "tier_id": tier_id,
            "identifier_codes": ["C-1", "C-2", "C-1"],
        },
    )
    assert res_dup_in_req.status_code == 422
    assert "Duplicate identifier codes" in res_dup_in_req.text


# 12. max_capacity is rejected when booking_mode is unit_assigned (prevents dual source of truth)
async def test_create_service_unit_assigned_rejects_max_capacity(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
):
    loc_id = await create_test_location(client, seller_headers)

    res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Conflicting Capacity Event",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "max_capacity": 200,
            "base_price": 50.00,
        **make_test_time_window(),
        },
    )
    assert res.status_code == 400
    assert "max_capacity cannot be set for unit_assigned booking mode" in res.json()["detail"]


# 13. Cross-seller cannot add tiers or bulk inventory to another seller's service (403)
async def test_cross_seller_service_modification_forbidden(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    db_session,
):
    from datetime import date
    from app import oauth2, utils

    loc_id = await create_test_location(client, seller_headers)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Seller 1 Private Gala",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 100.00,
        **make_test_time_window(),
        },
    )
    service_id = service_res.json()["id"]

    # Create Seller 2
    seller_2 = model.User(
        name="Second Seller",
        email="seller2_services@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1990, 5, 5),
        role=model.UserRole.SELLER,
        phone_no="+1888888801",
        is_verified=True,
    )
    db_session.add(seller_2)
    db_session.commit()
    db_session.refresh(seller_2)

    profile_2 = model.SellerProfile(
        user_id=seller_2.id,
        business_name="Seller 2 Entertainment",
    )
    db_session.add(profile_2)
    db_session.commit()
    db_session.refresh(seller_2)

    token_2 = oauth2.create_access_token(data={"user_id": seller_2.id, "sub": str(seller_2.id)})
    seller_2_headers = {"Authorization": f"Bearer {token_2}"}

    # Seller 2 tries to add a tier to Seller 1's service
    tier_res = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_2_headers,
        json={"name": "Hacked Tier", "price": 10.00},
    )
    assert tier_res.status_code == 403
    assert "Not authorized" in tier_res.json()["detail"]

    # Seller 2 tries to add bulk inventory to Seller 1's service
    inv_res = await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_2_headers,
        json={"tier_id": 1, "identifier_codes": ["H-1"]},
    )
    assert inv_res.status_code == 403
    assert "Not authorized" in inv_res.json()["detail"]

# ==========================================================
# PUBLIC DISCOVERY & LIVE SEAT MAP TESTS
# ==========================================================

async def test_public_get_all_services(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    loc_id = await create_test_location(client, seller_headers)

    # Event 1: Tech Conference (slot_capacity, $50)
    await client.post("/services", headers=seller_headers, json={
        "service_name": "Tech Conference 2026",
        "service_desc": "Annual Developers Summit",
        "location_id": loc_id,
        "booking_mode": "slot_capacity",
        "max_capacity": 500,
        "base_price": 50.00,
        **make_test_time_window()
    })

    # Event 2: Rock Concert (unit_assigned, $150)
    await client.post("/services", headers=seller_headers, json={
        "service_name": "Rock Concert",
        "service_desc": "Live music event",
        "location_id": loc_id,
        "booking_mode": "unit_assigned",
        "max_capacity": None,
        "base_price": 150.00,
        **make_test_time_window()
    })

    # 1. Fetch all services (No authentication needed!)
    res = await client.get("/services")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 2

    # 2. Test Filtering by booking_mode
    res_filter = await client.get("/services?booking_mode=unit_assigned")
    assert res_filter.status_code == 200
    data_filter = res_filter.json()
    for event in data_filter:
        assert event["booking_mode"] == "unit_assigned"

    # 3. Test Filtering by Max Price
    res_price = await client.get("/services?max_price=100")
    assert res_price.status_code == 200
    data_price = res_price.json()
    # Should only return the $50 Tech Conference, not the $150 Rock Concert
    assert any(e["service_name"] == "Tech Conference 2026" for e in data_price)
    assert not any(e["service_name"] == "Rock Concert" for e in data_price)


async def test_public_get_live_seat_map(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    # Setup: Create a unit_assigned event
    loc_id = await create_test_location(client, seller_headers)
    res_svc = await client.post("/services", headers=seller_headers, json={
        "service_name": "Seat Map Test Event",
        "service_desc": "Checking the visual map",
        "location_id": loc_id,
        "booking_mode": "unit_assigned",
        "base_price": 100.00,
        **make_test_time_window()
    })
    service_id = res_svc.json()["id"]

    # Fix: Create a pricing tier first!
    res_tier = await client.post(
        f"/services/{service_id}/tiers",
        headers=seller_headers,
        json={"name": "VIP", "price": 150.00}
    )
    tier_id = res_tier.json()["id"]

    # Generate 5 seats assigned to that tier
    await client.post(
        f"/services/{service_id}/inventory/bulk",
        headers=seller_headers,
        json={"tier_id": tier_id, "identifier_codes": ["A-1", "A-2", "A-3", "A-4", "A-5"]}
    )

    # Fetch the live seat map!
    res_seats = await client.get(f"/services/{service_id}/seats")
    assert res_seats.status_code == 200
    seats = res_seats.json()

    assert len(seats) == 5
    # Ensure it returns the identifier codes, status, and the tier!
    assert seats[0]["identifier_code"] == "A-1"
    assert seats[0]["status"] == "available"
    assert seats[0]["tier_id"] == tier_id


async def test_public_get_seat_map_slot_capacity_rejected(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    # Setup: Create a slot_capacity event (General Admission)
    loc_id = await create_test_location(client, seller_headers)
    res_svc = await client.post("/services", headers=seller_headers, json={
        "service_name": "General Admission Event",
        "service_desc": "No specific seats",
        "location_id": loc_id,
        "booking_mode": "slot_capacity",
        "max_capacity": 100,
        "base_price": 50.00,
        **make_test_time_window()
    })
    service_id = res_svc.json()["id"]
    # Try to fetch a seat map for a General Admission event (should fail with 400 Bad Request)
    res_seats = await client.get(f"/services/{service_id}/seats")
    assert res_seats.status_code == 400
    assert "seat maps are only available" in res_seats.json()["detail"].lower()


# 17. Creating service with end_time <= start_time is rejected (422)
async def test_create_service_invalid_time_range_rejected(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    loc_id = await create_test_location(client, seller_headers)
    now = datetime.now(timezone.utc)
    res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Backwards Time Travel Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 100,
            "base_price": 50.00,
            "start_time": (now + timedelta(days=5)).isoformat(),
            "end_time": (now + timedelta(days=4)).isoformat()  # End before start!
        }
    )
    assert res.status_code == 422
    assert "end_time must be strictly after start_time" in res.text


# 18. Public service discovery date filtering and date sorting
async def test_public_get_services_date_filtering_and_sorting(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    loc_id = await create_test_location(client, seller_headers)
    now = datetime.now(timezone.utc)

    # Event in 2 days
    await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Near Future Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 100,
            "base_price": 30.00,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=3)).isoformat()
        }
    )

    # Event in 10 days
    await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Far Future Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 100,
            "base_price": 70.00,
            "start_time": (now + timedelta(days=10)).isoformat(),
            "end_time": (now + timedelta(days=10, hours=3)).isoformat()
        }
    )

    # 1. Filter by date_to = now + 5 days (should return Near Future Event, not Far Future Event)
    res_date = await client.get("/services", params={"date_to": (now + timedelta(days=5)).isoformat()})
    assert res_date.status_code == 200
    names = [e["service_name"] for e in res_date.json()]
    assert "Near Future Event" in names
    assert "Far Future Event" not in names

    # 2. Sort by date_asc
    res_asc = await client.get("/services?sort_by=date_asc")
    assert res_asc.status_code == 200
    events_asc = res_asc.json()
    start_times = [e["start_time"] for e in events_asc]
    assert start_times == sorted(start_times)

    # 3. Sort by date_desc
    res_desc = await client.get("/services?sort_by=date_desc")
    assert res_desc.status_code == 200
    events_desc = res_desc.json()
    start_times_desc = [e["start_time"] for e in events_desc]
    assert start_times_desc == sorted(start_times_desc, reverse=True)
