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

# Verified Seller creates a slot_capacity event
async def test_create_service_slot_capacity(
    client: httpx.AsyncClient,
    seller_user: model.User,
    seller_headers: dict[str, str]
):
    location_id = create_test_location(client, seller_headers)
    
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
    data = responses