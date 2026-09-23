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
        json={"city": "Faisalabad", "country": "Pakistan", "address_line": "Iqbal Cricket Stadium"}
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
    assert any("Footbal" in addr for addr in addresses)

    # 4. Search for something non-existent and it returns empty list
    res_empty = await client.get("/locations?search=cricket")
    assert res_empty.status_code == 200
    assert len(res_empty.json()) == 0