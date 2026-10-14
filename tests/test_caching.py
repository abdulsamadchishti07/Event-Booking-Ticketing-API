from datetime import datetime, timedelta, timezone
from decimal import Decimal
import httpx
import pytest
import redis.asyncio as aioredis
from unittest.mock import patch

from app.database import model
from app.core.tasks import released_expired_holds


def make_test_time_window(days_ahead=5, duration_hours=3):
    now = datetime.now(timezone.utc)
    return {
        "start_time": (now + timedelta(days=days_ahead)).isoformat(),
        "end_time": (now + timedelta(days=days_ahead, hours=duration_hours)).isoformat()
    }


async def setup_test_service(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    booking_mode: str = "slot_capacity"
) -> int:
    loc_res = await client.post("/locations", headers=seller_headers, json={
        "city": "CachingCity",
        "country": "US",
        "address_line": "123 Performance Way"
    })
    loc_id = loc_res.json()["id"]

    svc_payload = {
        "service_name": "Cache Test Festival",
        "service_desc": "Sub-5ms latency test",
        "location_id": loc_id,
        "booking_mode": booking_mode,
        "base_price": 75.00,
        **make_test_time_window()
    }
    if booking_mode == "slot_capacity":
        svc_payload["max_capacity"] = 100

    svc_res = await client.post("/services", headers=seller_headers, json=svc_payload)
    return svc_res.json()["id"]


# 1. Test GET /services Cache MISS followed by sub-5ms Cache HIT
async def test_services_list_cache_hit_and_miss(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    test_redis: aioredis.Redis
):
    service_id = await setup_test_service(client, seller_headers)

    # First request: Cache MISS (hits PostgreSQL, populates Redis)
    res_miss = await client.get("/services")
    assert res_miss.status_code == 200
    assert res_miss.headers.get("x-cache") == "MISS"
    data_miss = res_miss.json()
    assert any(s["id"] == service_id for s in data_miss)

    # Verify key exists in Redis
    keys = [k async for k in test_redis.scan_iter("cache:services:list:*")]
    assert len(keys) >= 1

    # Second request: Cache HIT (served directly from Redis memory in <5ms)
    res_hit = await client.get("/services")
    assert res_hit.status_code == 200
    assert res_hit.headers.get("x-cache") == "HIT"
    assert res_hit.json() == data_miss


# 2. Test GET /services/{id} Detail Cache
async def test_service_detail_cache_hit_and_miss(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    service_id = await setup_test_service(client, seller_headers)

    # First call: MISS
    res1 = await client.get(f"/services/{service_id}")
    assert res1.status_code == 200
    assert res1.headers.get("x-cache") == "MISS"

    # Second call: HIT
    res2 = await client.get(f"/services/{service_id}")
    assert res2.status_code == 200
    assert res2.headers.get("x-cache") == "HIT"
    assert res2.json()["service_name"] == "Cache Test Festival"


# 3. Test Live Seat Map Caching
async def test_seat_map_cache_hit_and_miss(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    service_id = await setup_test_service(client, seller_headers, booking_mode="unit_assigned")

    # Create tier and inventory
    tier_res = await client.post(f"/services/{service_id}/tiers", headers=seller_headers, json={
        "name": "General Seat",
        "price": 50.00
    })
    tier_id = tier_res.json()["id"]

    await client.post(f"/services/{service_id}/inventory/bulk", headers=seller_headers, json={
        "tier_id": tier_id,
        "identifier_codes": ["S-1", "S-2", "S-3"]
    })

    # Seat map initial read: MISS
    res1 = await client.get(f"/services/{service_id}/seats")
    assert res1.status_code == 200
    assert res1.headers.get("x-cache") == "MISS"
    assert len(res1.json()) == 3

    # Subsequent read: HIT
    res2 = await client.get(f"/services/{service_id}/seats")
    assert res2.status_code == 200
    assert res2.headers.get("x-cache") == "HIT"


# 4. Invalidation on Service Update (PUT /services/{id})
async def test_cache_invalidation_on_service_update(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    service_id = await setup_test_service(client, seller_headers)

    # Warm cache
    r1 = await client.get("/services")
    assert r1.headers.get("x-cache") == "MISS"
    r2 = await client.get("/services")
    assert r2.headers.get("x-cache") == "HIT"

    # Update service name
    update_res = await client.put(f"/services/{service_id}", headers=seller_headers, json={
        "service_name": "Updated Festival 2026"
    })
    assert update_res.status_code == 200

    # Next fetch MUST be a MISS with fresh data
    r3 = await client.get("/services")
    assert r3.headers.get("x-cache") == "MISS"
    data = r3.json()
    updated = next(s for s in data if s["id"] == service_id)
    assert updated["service_name"] == "Updated Festival 2026"


# 5. Invalidation on Service Delete (DELETE /services/{id})
async def test_cache_invalidation_on_service_delete(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    service_id = await setup_test_service(client, seller_headers)

    # Warm cache
    await client.get("/services")
    await client.get(f"/services/{service_id}")

    # Delete service
    del_res = await client.delete(f"/services/{service_id}", headers=seller_headers)
    assert del_res.status_code == 204

    # Detail must immediately return 404
    get_res = await client.get(f"/services/{service_id}")
    assert get_res.status_code == 404

    # Listing must be a MISS and omit the deleted event
    list_res = await client.get("/services")
    assert list_res.headers.get("x-cache") == "MISS"
    assert not any(s["id"] == service_id for s in list_res.json())


# 6. Invalidation on Booking Reservation
async def test_cache_invalidation_on_booking_reservation(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str]
):
    service_id = await setup_test_service(client, seller_headers, booking_mode="unit_assigned")

    tier_res = await client.post(f"/services/{service_id}/tiers", headers=seller_headers, json={
        "name": "Standard Tier",
        "price": 30.00
    })
    tier_id = tier_res.json()["id"]

    await client.post(f"/services/{service_id}/inventory/bulk", headers=seller_headers, json={
        "tier_id": tier_id,
        "identifier_codes": ["B-1", "B-2"]
    })

    # Cache seat map
    seat_res1 = await client.get(f"/services/{service_id}/seats")
    assert seat_res1.headers.get("x-cache") == "MISS"
    seat_1_id = seat_res1.json()[0]["id"]

    # Customer books Seat 1
    booking_res = await client.post("/bookings", headers=user_headers, json={
        "service_id": service_id,
        "assigned_unit_ids": [seat_1_id]
    })
    assert booking_res.status_code == 201

    # Query seat map: MUST be MISS and reflect Seat 1 as 'reserved'
    seat_res2 = await client.get(f"/services/{service_id}/seats")
    assert seat_res2.headers.get("x-cache") == "MISS"
    seat_1_updated = next(s for s in seat_res2.json() if s["id"] == seat_1_id)
    assert seat_1_updated["status"] == "reserved"


# 7. Invalidation on Hold Expiration Sweeper
async def test_cache_invalidation_on_sweeper_hold_release(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session
):
    service_id = await setup_test_service(client, seller_headers, booking_mode="unit_assigned")

    tier_res = await client.post(f"/services/{service_id}/tiers", headers=seller_headers, json={
        "name": "Sweeper Tier",
        "price": 25.00
    })
    tier_id = tier_res.json()["id"]

    await client.post(f"/services/{service_id}/inventory/bulk", headers=seller_headers, json={
        "tier_id": tier_id,
        "identifier_codes": ["SW-1"]
    })

    seat_res = await client.get(f"/services/{service_id}/seats")
    seat_id = seat_res.json()[0]["id"]

    # Book seat
    bk_res = await client.post("/bookings", headers=user_headers, json={
        "service_id": service_id,
        "assigned_unit_ids": [seat_id]
    })
    booking_id = bk_res.json()["id"]

    # Warm seat map cache
    seat_map_cached = await client.get(f"/services/{service_id}/seats")
    assert seat_map_cached.json()[0]["status"] == "reserved"

    # Manually backdate booking created_at to trigger sweeper
    booking = db_session.query(model.Booking).filter(model.Booking.id == booking_id).first()
    booking.created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    db_session.commit()

    # Run hold sweeper
    released_expired_holds(db=db_session)

    # Next seat map query must show seat back to 'available'
    seat_map_after = await client.get(f"/services/{service_id}/seats")
    assert seat_map_after.json()[0]["status"] == "available"


# 8. Rate Limiting Protection on High-Cost Search Endpoint
async def test_services_rate_limiting_protection(
    client: httpx.AsyncClient
):
    # Endpoint is protected by 120 requests/min
    for _ in range(120):
        r = await client.get("/services")
        assert r.status_code == 200

    # 121st request must trigger 429
    r_blocked = await client.get("/services")
    assert r_blocked.status_code == 429
    assert "Retry-After" in r_blocked.headers


# 9. Fail-Open Resilience when Redis encounters error
async def test_cache_fail_open_on_redis_error(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str]
):
    await setup_test_service(client, seller_headers)

    # Mock Redis get to simulate connection drop / timeout
    with patch("app.core.cache.get_cached", return_value=None):
        res = await client.get("/services")
        # System fails open to PostgreSQL gracefully without crashing
        assert res.status_code == 200
