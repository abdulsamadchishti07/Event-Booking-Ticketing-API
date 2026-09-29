import httpx
import pytest

from app import model


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