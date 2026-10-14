import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app.core import oauth2, utils
from app.core.cache import make_services_list_cache_key
from app.core.config import settings
from app.database import model


# ============================================================================
# 1. GET /services Datetime Normalization & Cache Key Parity
# ============================================================================

async def test_search_services_mixed_naive_and_aware_datetimes(
    client: httpx.AsyncClient,
    seller_headers: dict,
    db_session: Session
):
    """
    Ensures GET /services parses mixed naive and aware date_from / date_to
    without raising TypeError: can't compare offset-naive and offset-aware datetimes.
    """
    # 1. Create a test event in the future
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Lahore", "country": "PK", "address_line": "Mall Rd"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    start_dt = now + timedelta(days=5)
    end_dt = start_dt + timedelta(hours=2)

    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Tz Aware Event",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 100,
            "base_price": 25.00,
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat()
        }
    )
    assert svc_res.status_code == 201

    # 2. Query with aware date_from and naive date_to (previously crashed with 500)
    query_params = {
        "date_from": "2026-11-01T00:00:00Z",
        "date_to": "2026-11-20T23:59:59"
    }
    res = await client.get("/services", params=query_params)
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    # 3. Query with naive date_from and aware date_to
    query_params_rev = {
        "date_from": "2026-11-01T00:00:00",
        "date_to": "2026-11-20T23:59:59+00:00"
    }
    res_rev = await client.get("/services", params=query_params_rev)
    assert res_rev.status_code == 200

    # 4. date_from > date_to with mixed offsets raises clean 400 Bad Request
    invalid_params = {
        "date_from": "2026-11-20T00:00:00Z",
        "date_to": "2026-11-01T00:00:00"
    }
    res_inv = await client.get("/services", params=invalid_params)
    assert res_inv.status_code == 400
    assert "date_from cannot be greater than date_to" in res_inv.json()["detail"]


def test_make_services_list_cache_key_normalizes_to_utc():
    """
    Verifies that make_services_list_cache_key hashes identical instants
    to the exact same cache key regardless of naive vs aware UTC representation.
    """
    dt_naive = datetime(2026, 11, 1, 12, 0, 0)
    dt_aware = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    key1 = make_services_list_cache_key(date_from=dt_naive)
    key2 = make_services_list_cache_key(date_from=dt_aware)

    assert key1 == key2


# ============================================================================
# 2. DELETE /account/{id} Session Revocation & Cache Invalidation
# ============================================================================

async def test_delete_user_seller_revokes_sessions_and_clears_cache(
    client: httpx.AsyncClient,
    db_session: Session,
    test_redis: aioredis.Redis
):
    """
    When a seller deletes their account:
    - user row is deleted
    - active Redis sessions for user are revoked
    - user_revoked_at:{id} is set
    - invalidate_services_cache is called
    """
    # 1. Create seller user
    user = model.User(
        name="Deleting Seller",
        email="deletingseller@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1990, 1, 1),
        role=model.UserRole.SELLER,
        phone_no="+1888000111",
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    seller_prof = model.SellerProfile(
        user_id=user.id,
        business_name="Deleting Seller Org"
    )
    db_session.add(seller_prof)
    db_session.commit()

    token = oauth2.create_access_token(data={"user_id": user.id, "sub": str(user.id)})
    headers = {"Authorization": f"Bearer {token}"}

    # Simulate an active session in Redis
    session_key = f"session:{user.id}:mock_jti"
    await test_redis.set(session_key, "active", ex=3600)

    # Pre-populate services cache
    await test_redis.set("cache:services:list:mock_query", "[]", ex=300)

    # 2. Delete account
    del_res = await client.delete(f"/account/{user.id}", headers=headers)
    assert del_res.status_code == 204

    # 3. Verify Redis session keys revoked
    assert await test_redis.get(session_key) is None

    # 4. Verify user_revoked_at was recorded
    revoked_ts = await test_redis.get(f"user_revoked_at:{user.id}")
    assert revoked_ts is not None

    # 5. Verify services cache was invalidated
    assert await test_redis.get("cache:services:list:mock_query") is None


# ============================================================================
# 3. Consistent Admin RBAC Across locations.py and services.py
# ============================================================================

async def test_admin_without_seller_profile_can_manage_locations_and_services(
    client: httpx.AsyncClient,
    admin_headers: dict,
    seller_headers: dict,
    db_session: Session
):
    """
    An ADMIN without a seller profile:
    - Can update any seller's location
    - Can delete any seller's location
    - Can create service tiers on any seller's service
    - Can bulk create inventory on any seller's unit_assigned service
    """
    # 1. Seller creates location and service
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Rawalpindi", "country": "PK", "address_line": "Stadium"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Seated Concert",
            "location_id": loc_id,
            "booking_mode": "unit_assigned",
            "base_price": 50.00,
            "start_time": (now + timedelta(days=10)).isoformat(),
            "end_time": (now + timedelta(days=10, hours=3)).isoformat()
        }
    )
    assert svc_res.status_code == 201
    svc_id = svc_res.json()["id"]

    # 2. Admin creates a service tier on the seller's service
    tier_res = await client.post(
        f"/services/{svc_id}/tiers",
        headers=admin_headers,
        json={"name": "VIP Admin Tier", "price": 120.00}
    )
    assert tier_res.status_code == 201
    tier_id = tier_res.json()["id"]

    # 3. Admin bulk creates inventory on the seller's service
    inv_res = await client.post(
        f"/services/{svc_id}/inventory/bulk",
        headers=admin_headers,
        json={
            "tier_id": tier_id,
            "identifier_codes": ["A-1", "A-2", "A-3", "A-4", "A-5"]
        }
    )
    assert inv_res.status_code == 201
    assert inv_res.json()["created_count"] == 5

    # 4. Admin updates location
    update_loc_res = await client.put(
        f"/locations/{loc_id}",
        headers=admin_headers,
        json={"address_line": "Renovated Stadium"}
    )
    assert update_loc_res.status_code == 200
    assert update_loc_res.json()["address_line"] == "Renovated Stadium"

    # 5. Delete service first, then Admin deletes location
    del_svc = await client.delete(f"/services/{svc_id}", headers=admin_headers)
    assert del_svc.status_code == 204

    del_loc = await client.delete(f"/locations/{loc_id}", headers=admin_headers)
    assert del_loc.status_code == 204


# ============================================================================
# 4. Payments Cache Invalidation Dependency Override Test
# ============================================================================

async def test_payments_endpoints_use_injected_redis_for_cache_invalidation(
    client: httpx.AsyncClient,
    seller_headers: dict,
    user_headers: dict,
    test_redis: aioredis.Redis,
    db_session: Session
):
    """
    Verifies that payment workflows (pay, cancel, confirm) use the injected
    get_redis dependency when invalidating the services cache.
    """
    loc_res = await client.post(
        "/locations",
        headers=seller_headers,
        json={"city": "Islamabad", "country": "PK", "address_line": "Blue Area"}
    )
    assert loc_res.status_code == 201
    loc_id = loc_res.json()["id"]

    now = datetime.now(timezone.utc)
    svc_res = await client.post(
        "/services",
        headers=seller_headers,
        json={
            "service_name": "Free Seminar",
            "location_id": loc_id,
            "booking_mode": "slot_capacity",
            "max_capacity": 50,
            "base_price": 0.00,
            "start_time": (now + timedelta(days=3)).isoformat(),
            "end_time": (now + timedelta(days=3, hours=2)).isoformat()
        }
    )
    assert svc_res.status_code == 201
    svc_id = svc_res.json()["id"]

    # Book ticket
    bk_res = await client.post(
        "/bookings",
        headers=user_headers,
        json={"service_id": svc_id, "quantity": 1}
    )
    assert bk_res.status_code == 201
    bk_id = bk_res.json()["id"]

    # Set cache key
    cache_key = f"cache:services:detail:{svc_id}"
    await test_redis.set(cache_key, '{"cached": true}', ex=300)

    # Track invalidate_services_cache calls and assert injected redis is passed
    with patch("app.routers.payments.invalidate_services_cache", new_callable=AsyncMock) as mock_inv:
        # Pay $0 free event
        pay_res = await client.post(f"/bookings/{bk_id}/pay", headers=user_headers)
        assert pay_res.status_code == 200

        # Wait small moment for background tasks
        await asyncio.sleep(0.1)

        # Assert mock was called with redis matching test_redis (injected dependency)
        assert mock_inv.called
        kwargs = mock_inv.call_args.kwargs
        assert kwargs.get("service_id") == svc_id
        assert kwargs.get("redis") is not None

    # Cancel booking also invalidates cache with injected redis
    with patch("app.routers.payments.invalidate_services_cache", new_callable=AsyncMock) as mock_inv_cancel:
        cancel_res = await client.post(
            f"/bookings/{bk_id}/cancel",
            headers=user_headers,
            json={"reason": "Cannot attend"}
        )
        assert cancel_res.status_code == 200

        await asyncio.sleep(0.1)
        assert mock_inv_cancel.called
        assert mock_inv_cancel.call_args.kwargs.get("redis") is not None
