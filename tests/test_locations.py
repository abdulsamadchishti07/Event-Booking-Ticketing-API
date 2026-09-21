import httpx
import pytest
from sqlalchemy.orm import Session

from app import model


async def test_location(
    client: httpx.AsyncClient,
    seller_user: model.User,
    seller_headers: dict[str, str]
):
    # Venu Data
    payload = {
        "city": "Bahawalpur",
        "country": "Pakistan",
        "adress_line": "Star City"
    }

    # is seller verified
    response = await client.post("/locations", headers=seller_headers, json=payload)

    # status and respose code
    assert response.status_code == 201
    data = response.json()
    assert data["city"] == "Bahawalpur"
    assert data["country"] == "Pakistan"
    assert data["adress_line"] == "Star City"
    assert data["seller_id"] == seller_user.seller_profile.id
    assert "id" in data