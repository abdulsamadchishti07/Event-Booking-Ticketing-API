from datetime import datetime, timedelta, timezone
from redis import client
from fastapi import responses
import httpx
import pytest
from sqlalchemy.orm import Session

from app import model

# verified seller creates a venue successfully:
async def test_location(
    client: httpx.AsyncClient,
    seller_user: model.User,
    seller_headers: dict[str, str]
):
    # Venu Data
    payload = {
        "city": "Bahawalpur",
        "country": "Pakistan",
        "address_line": "Star City"
    }

    # is seller verified
    response = await client.post("/locations", headers=seller_headers, json=payload)

    # status and respose code
    assert response.status_code == 201
    data = response.json() 
    

    assert data["city"] == "Bahawalpur"
    assert data["country"] == "Pakistan"
    assert data["address_line"] == "Star City"
    assert data["seller_id"] == seller_user.seller_profile.id
    assert "id" in data

# Customer is Forbidden
async def test_create_losation_customer_forbidded(
    client: httpx.AsyncClient,
    user_headers: dict[str, str]
):
    payload = {
        "city": "Bahawalpur",
        "country": "Pakistan",
        "address_line": "Star City",
    }

    response = await client.post("/locations", headers=user_headers, json=payload)
    
    assert response.status_code == 403

# Unauthenticated Guest is Rejected
async def test_create_location_unauthenticated(
    client: httpx.AsyncClient
):
    payload = {
        "city": "Bahawalpur",
        "country": "Pakistan",
        "address_line": "Star City"    
    }

    # No header send
    response = await client.post("/locations", json=payload)

    assert response.status_code == 401

# all locations
async def test_all_location(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    payload = {
        "city": "Lahore",
        "country": "Pakistan",
        "address_line": "Gaddafi Stadium"
    }
    create_res = await client.post(
        "/locations", headers=seller_headers, json=payload
    )

    assert create_res.status_code == 201

    # get request (no headers/ unauthentication)
    response = await client.get("/locations")
    assert response.status_code == 200

    data = response.json()

    assert isinstance(data, list)
    assert len(data) >=1

    cities = [loc["city"] for loc in data]
    assert "Lahore" in cities

# get location by id or get 404 
async def test_get_location_by_id(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    payload = {
        "city": "Karachi",
        "country": "Pakistan",
        "address_line": "National Stadium"
    }
    
    create_res = await client.post("/locations", headers=seller_headers, json=payload)
    assert create_res.status_code == 201
    created_id = create_res.json()["id"]
    
    
    response = await client.get(f"/locations/{created_id}")
    assert response.status_code == 200
    data = response.json()
    
    assert data["id"] == created_id
    assert data["city"] == "Karachi"
    

    not_found_res = await client.get("/locations/999999")
    assert not_found_res.status_code == 404

# Test Update Location
async def test_update_location(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):

    create_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Multan", "country": "Pakistan", "address_line": "Old Address"}
    )
    assert create_res.status_code == 201
    loc_id = create_res.json()["id"]

    # Customer tries to update
    cust_res = await client.put(
        f"/locations/{loc_id}",
        headers=user_headers,
        json={"address_line": "Customer Hacked Address"}
    )
    assert cust_res.status_code == 403

    # Owner updates address
    update_res = await client.put(
        f"/locations/{loc_id}",
        headers=seller_headers,
        json={"address_line": "Updated New Address"}
    )
    assert update_res.status_code == 200
    assert update_res.json()["address_line"] == "Updated New Address"
    assert update_res.json()["city"] == "Multan"

    # Non-existent location
    not_found_res = await client.put(
        "/locations/999999",
        headers=seller_headers,
        json={"city": "Nowhere"}
    )
    assert not_found_res.status_code == 404


# Test Delete Location
async def test_delete_location(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):

    create_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Peshawar", "country": "Pakistan", "address_line": "Stadium Rd"}
    )
    assert create_res.status_code == 201
    loc_id = create_res.json()["id"]

    cust_del = await client.delete(f"/locations/{loc_id}", headers=user_headers)
    assert cust_del.status_code == 403

    # Owner deletes location
    del_res = await client.delete(f"/locations/{loc_id}", headers=seller_headers)
    assert del_res.status_code == 204

    # Verify it is really gone 
    get_res = await client.get(f"/locations/{loc_id}")
    assert get_res.status_code == 404

    # Deleting non-existent location 
    del_not_found = await client.delete("/locations/999999", headers=seller_headers)
    assert del_not_found.status_code == 404

# Test Search by partial city, country, or keyword
async def test_search_location(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Faisalabad", "country": "Pakistan", "address_line": "Iqbal football Stadium"}
    )
    # 2. Search by  lowercase city
    res_city = await client.get("/locations?city=faisal")
    assert res_city.status_code == 200
    cities = [loc["city"] for loc in res_city.json()]
    assert "Faisalabad" in cities

    # 3. Universal search by address keyword: "Football"
    res_search = await client.get("/locations?search=football")
    assert res_search.status_code == 200
    addresses = [loc["address_line"] for loc in res_search.json()]
    assert any("football" in addr for addr in addresses)

    # 4. Search for something non-existent and it returns empty list
    res_empty = await client.get("/locations?search=nonexistent_location_query_xyz123")
    assert res_empty.status_code == 200
    assert len(res_empty.json()) == 0


# Test: delete_location ownership check runs BEFORE business logic check (fail closed, no info leak)
async def test_delete_location_ownership_check_runs_before_business_rule(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    db_session: Session
):
    # 1. Seller A creates a location
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "Pakistan", "address_line": "F-9 Park"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    service_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Park Festival",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 500,
            "base_price": 20.00,
            "start_time": (now + timedelta(days=1)).isoformat(),
            "end_time": (now + timedelta(days=1, hours=4)).isoformat()
        }
    )
    assert service_res.status_code == 201

    # 3. Create a Second Seller (Seller B)
    from datetime import date
    from app import oauth2, utils

    seller_b = model.User(
        name="Second Seller",
        email="seller_b@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1991, 1, 1),
        role=model.UserRole.SELLER,
        phone_no="+1999999901",
        is_verified=True,
    )
    db_session.add(seller_b)
    db_session.commit()
    db_session.refresh(seller_b)

    seller_b_profile = model.SellerProfile(
        user_id=seller_b.id,
        business_name="Seller B Biz",
    )
    db_session.add(seller_b_profile)
    db_session.commit()
    db_session.refresh(seller_b)

    token_b = oauth2.create_access_token(data={"user_id": seller_b.id, "sub": str(seller_b.id)})
    seller_b_headers = {"Authorization": f"Bearer {token_b}"}

    # 4. Seller B tries to delete Seller A's location
    # Ownership check MUST run before business rule check -> returns 403, NOT 400
    res_b = await client.delete(f"/locations/{loc_id}", headers=seller_b_headers)
    assert res_b.status_code == 403
    assert "Not authorized" in res_b.json()["detail"]

    # 5. Seller A (the actual owner) attempts to delete it -> gets 400 because active service is scheduled
    res_a = await client.delete(f"/locations/{loc_id}", headers=seller_headers)
    assert res_a.status_code == 400
    assert "active events/services" in res_a.json()["detail"]


# Test: update_location and delete_location return clean 400 if user has SELLER role but no SellerProfile
async def test_update_and_delete_location_seller_without_profile_returns_400(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    db_session: Session
):
    from datetime import date
    from app import oauth2, utils

    # Create location with valid seller
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Quetta", "country": "Pakistan", "address_line": "Chaman Rd"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    # Create user with SELLER role but NO SellerProfile
    seller_no_profile = model.User(
        name="Incomplete Seller",
        email="incomplete_seller@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1992, 2, 2),
        role=model.UserRole.SELLER,
        phone_no="+1999999902",
        is_verified=True,
    )
    db_session.add(seller_no_profile)
    db_session.commit()
    db_session.refresh(seller_no_profile)

    token = oauth2.create_access_token(data={"user_id": seller_no_profile.id, "sub": str(seller_no_profile.id)})
    incomplete_headers = {"Authorization": f"Bearer {token}"}

    # Attempt PUT
    put_res = await client.put(
        f"/locations/{loc_id}",
        headers=incomplete_headers,
        json={"city": "Hacked City"}
    )
    assert put_res.status_code == 400
    assert "does not have seller profile" in put_res.json()["detail"]

    # Attempt DELETE
    del_res = await client.delete(f"/locations/{loc_id}", headers=incomplete_headers)
    assert del_res.status_code == 400
    assert "does not have seller profile" in del_res.json()["detail"]


# Test: get_locations limit parameter has upper and lower bounds (ge=1, le=100)
async def test_get_locations_limit_bounds(client: httpx.AsyncClient):
    # Over maximum limit (100) -> 422 Unprocessable Entity
    res_high = await client.get("/locations?limit=999999")
    assert res_high.status_code == 422

    # Under minimum limit (1) -> 422 Unprocessable Entity
    res_low = await client.get("/locations?limit=0")
    assert res_low.status_code == 422

    # Valid limit -> 200 OK
    res_valid = await client.get("/locations?limit=50")
    assert res_valid.status_code == 200