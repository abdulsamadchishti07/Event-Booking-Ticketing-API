import httpx
import pytest

from app.database import model



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
        "base_price": 50.00
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
        "base_price": 100.00
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
        "base_price": 10.00
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
        "base_price": 10.00
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
            "base_price": 40.00
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
            "base_price": 20.00
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
            "base_price": 15.00
        }
    )
    service_id = service_res.json()["id"]

    response = await client.post(
        f"/services/{service_id}/tiers",
        headers=user_headers,
        json={"name": "Gold", "price": 30.00}
    )
    assert response.status_code == 403
