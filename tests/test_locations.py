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
