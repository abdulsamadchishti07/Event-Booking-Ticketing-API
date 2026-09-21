from pydantic_settings.sources.providers import json
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
    data = response.json() # unauthenticated request
    response = await client.post("/locations", json={
        "city": "Islamabad",
        "country": "Pakistan",
        "address_line": "Star City"
    })
    

    assert data["city"] == "Bahawalpur"
    assert data["country"] == "Pakistan"
    assert data["address_line"] == "Star City"
    assert data["seller_id"] == seller_user.seller_profile.id
    assert "id" in data

# Customer is Forbidden
async def test_create_losation_customer_forbidded(
    client: httpx.AsyncClient,
    user_header: dict[str, str]
):
    payload = {
        "city": "Bahawalpur",
        "country": "Pakistan",
        "address_line": "Star City",
    }

    response = await client.post("/locations", headers=user_header, json=payload)
    
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