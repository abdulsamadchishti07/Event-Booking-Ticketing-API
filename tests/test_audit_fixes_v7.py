import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import httpx
import pytest
from sqlalchemy.orm import Session

from app.database import model


@pytest.mark.asyncio
async def test_archived_event_endpoints_return_404_and_location_deletable(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    db_session: Session
):
    """
    Regression test for Issue 2:
    - Archived events return 404 on:
      1. GET /services/{id}
      2. GET /services/{id}/reviews
      3. PUT /services/{id}
      4. POST /services/{id}/tiers
      5. POST /services/{id}/inventory/bulk
      6. DELETE /services/{id}
    - Location can be deleted cleanly if all events at that location are archived.
    """
    now = datetime.now(timezone.utc)

    # 1. Create Location
    loc_resp = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Karachi", "country": "PK", "address_line": "Expo Hall 5"}
    )
    assert loc_resp.status_code == 201
    loc_id = loc_resp.json()["id"]

    # 2. Create Event
    svc_resp = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Archival Test Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 100,
            "base_price": 50.00,
            "start_time": (now + timedelta(days=5)).isoformat(),
            "end_time": (now + timedelta(days=5, hours=3)).isoformat()
        }
    )
    assert svc_resp.status_code == 201
    svc_id = svc_resp.json()["id"]

    # 3. Add Tier
    tier_resp = await client.post(
        f"/services/{svc_id}/tiers",
        headers=seller_headers,
        json={"name": "VIP", "price": 100.00}
    )
    assert tier_resp.status_code == 201
    tier_id = tier_resp.json()["id"]

    # 4. Create booking & cancel it so event has history and will be archived
    book_resp = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": svc_id, "quantity": 1}
    )
    assert book_resp.status_code == 201
    booking_id = book_resp.json()["id"]

    cancel_resp = await client.post(
        f"/bookings/{booking_id}/cancel",
        headers=user_headers,
        json={"reason": "Customer changed plans"}
    )
    assert cancel_resp.status_code == 200

    # 5. Delete event -> Should be archived (204)
    del_svc = await client.delete(f"/services/{svc_id}", headers=seller_headers)
    assert del_svc.status_code == 204

    # Verify event is marked is_active = False in database
    svc_row = db_session.query(model.Services).filter(model.Services.id == svc_id).first()
    assert svc_row is not None
    assert svc_row.is_active is False

    # 6. Check that ALL endpoints return 404 for this archived event
    # A. Detail
    assert (await client.get(f"/services/{svc_id}")).status_code == 404
    # B. Reviews
    assert (await client.get(f"/services/{svc_id}/reviews")).status_code == 404
    # C. Update service
    assert (await client.put(f"/services/{svc_id}", headers=seller_headers, json={"service_name": "New Name"})).status_code == 404
    # D. Add tier
    assert (await client.post(f"/services/{svc_id}/tiers", headers=seller_headers, json={"name": "Gold", "price": 75.0})).status_code == 404
    # E. Bulk inventory
    assert (await client.post(f"/services/{svc_id}/inventory/bulk", headers=seller_headers, json={"tier_id": tier_id, "identifier_codes": ["V3"]})).status_code == 404
    # F. Delete again
    assert (await client.delete(f"/services/{svc_id}", headers=seller_headers)).status_code == 404

    # 7. Delete Location -> Should SUCCEED (204) because all events at this location are archived
    del_loc = await client.delete(f"/locations/{loc_id}", headers=seller_headers)
    assert del_loc.status_code == 204

    # Verify location is deleted
    assert db_session.query(model.Location).filter(model.Location.id == loc_id).first() is None


@pytest.mark.asyncio
async def test_login_caps_concurrent_hashing_to_prevent_cpu_starvation(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis
):
    """
    Verifies that when active password verifications reach max_concurrent_auth_hashes,
    incoming login requests immediately return 429 with Retry-After header
    rather than stacking up and starving CPU.
    """
    from app.core.config import settings

    # Simulate saturated hashing pool
    await test_redis.set("concurrency:auth_hash", str(settings.max_concurrent_auth_hashes))

    resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert resp.status_code == 429
    assert resp.headers.get("retry-after") == "2"
    assert "Server is busy" in resp.json()["detail"]

    # When concurrency slot frees up, login succeeds immediately
    await test_redis.set("concurrency:auth_hash", "0")
    resp_ok = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert resp_ok.status_code == 200
    assert "access_token" in resp_ok.json()


@pytest.mark.asyncio
async def test_rate_limits_dynamically_configurable_by_env_settings(
    client: httpx.AsyncClient,
    test_redis
):
    """
    Verifies that auth and bookings rate limits are dynamic and read from settings,
    enabling single-machine load test runs with high limits without 429 errors.
    """
    from app.core.config import settings

    orig_auth_limit = settings.auth_rate_limit
    try:
        # Dynamically set auth rate limit to 2
        settings.auth_rate_limit = 2

        # 1st request
        r1 = await client.post("/login", data={"username": "fake@test.com", "password": "wrong"})
        assert r1.status_code in (401, 403, 404)

        # 2nd request
        r2 = await client.post("/login", data={"username": "fake@test.com", "password": "wrong"})
        assert r2.status_code in (401, 403, 404)

        # 3rd request -> hits rate limit of 2!
        r3 = await client.post("/login", data={"username": "fake@test.com", "password": "wrong"})
        assert r3.status_code == 429
        assert "Too many requests" in r3.json()["detail"]
    finally:
        settings.auth_rate_limit = orig_auth_limit

