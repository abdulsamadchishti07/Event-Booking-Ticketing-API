import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from app.core.config import settings
from app.core import redis as redis_module

logger = logging.getLogger(__name__)

CACHE_SERVICES_PREFIX = "cache:services"


def _get_active_redis_client(redis: Optional[aioredis.Redis] = None) -> aioredis.Redis:
    if redis is not None:
        return redis
    return redis_module.redis_client


def make_services_list_cache_key(
    city: Optional[str] = None,
    country: Optional[str] = None,
    booking_mode: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    include_past: bool = False,
    sort_by: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> str:
    """
    Generates a deterministic Redis cache key for event discovery searches.
    All filter parameters are normalized and hashed with SHA-256.
    """
    bm_str = (
        booking_mode.value if hasattr(booking_mode, "value") else str(booking_mode)
    ) if booking_mode else None

    def _norm_dt_str(dt: Optional[datetime]) -> Optional[str]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()

    params = {
        "city": city.strip().lower() if city else None,
        "country": country.strip().lower() if country else None,
        "booking_mode": bm_str,
        "min_price": round(float(min_price), 2) if min_price is not None else None,
        "max_price": round(float(max_price), 2) if max_price is not None else None,
        "date_from": _norm_dt_str(date_from),
        "date_to": _norm_dt_str(date_to),
        "include_past": bool(include_past),
        "sort_by": sort_by,
        "limit": limit,
        "offset": offset,
    }
    canonical = json.dumps(params, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{CACHE_SERVICES_PREFIX}:list:{digest}"


def make_service_detail_cache_key(service_id: int) -> str:
    return f"{CACHE_SERVICES_PREFIX}:detail:{service_id}"


def make_service_seats_cache_key(service_id: int) -> str:
    return f"{CACHE_SERVICES_PREFIX}:seats:{service_id}"


async def get_cached(
    key: str,
    redis: Optional[aioredis.Redis] = None
) -> Optional[str]:
    """
    Retrieves raw serialized JSON string from Redis cache.
    Fails open (returns None) if Redis is down or raises an error.
    """
    client = _get_active_redis_client(redis)
    try:
        return await client.get(key)
    except Exception as e:
        logger.warning(f"Redis get failed for key '{key}': {e}")
        return None


async def set_cached(
    key: str,
    value: str,
    ttl: int,
    redis: Optional[aioredis.Redis] = None
) -> bool:
    """
    Stores serialized JSON string in Redis cache with an expiration TTL.
    Fails open (returns False) if Redis encounters an error.
    """
    client = _get_active_redis_client(redis)
    try:
        await client.set(key, value, ex=ttl)
        return True
    except Exception as e:
        logger.warning(f"Redis set failed for key '{key}': {e}")
        return False


async def invalidate_pattern(
    pattern: str,
    redis: Optional[aioredis.Redis] = None
) -> int:
    """
    Scans and deletes all Redis keys matching the given pattern using non-blocking SCAN cursor.
    """
    client = _get_active_redis_client(redis)
    deleted = 0
    try:
        keys_to_delete = []
        async for key in client.scan_iter(match=pattern, count=100):
            keys_to_delete.append(key)
        if keys_to_delete:
            deleted = await client.delete(*keys_to_delete)
        return deleted
    except Exception as e:
        logger.warning(f"Redis invalidate_pattern failed for pattern '{pattern}': {e}")
        return 0


async def invalidate_services_cache(
    service_id: Optional[int] = None,
    redis: Optional[aioredis.Redis] = None
) -> int:
    """
    Invalidates event listing queries and specific service detail/seat map caches.
    - If service_id is provided: invalidates cache:services:detail:{service_id},
      cache:services:seats:{service_id}, and all cache:services:list:* queries.
    - If service_id is None: invalidates all cache:services:* keys.
    """
    client = _get_active_redis_client(redis)
    total_deleted = 0
    try:
        if service_id is not None:
            direct_keys = [
                make_service_detail_cache_key(service_id),
                make_service_seats_cache_key(service_id),
            ]
            total_deleted += await client.delete(*direct_keys)
            total_deleted += await invalidate_pattern(f"{CACHE_SERVICES_PREFIX}:list:*", redis=client)
        else:
            total_deleted += await invalidate_pattern(f"{CACHE_SERVICES_PREFIX}:*", redis=client)
        return total_deleted
    except Exception as e:
        logger.warning(f"Failed to invalidate services cache (service_id={service_id}): {e}")
        return 0


def _get_sync_redis_client(redis_inst: Optional[aioredis.Redis] = None):
    import redis as syncredis
    active = _get_active_redis_client(redis_inst)
    try:
        kwargs = dict(active.connection_pool.connection_kwargs)
        is_ssl = kwargs.get("ssl", False)
        connection_class = kwargs.get("connection_class")
        if connection_class and "ssl" in getattr(connection_class, "__name__", "").lower():
            is_ssl = True

        host = kwargs.get("host", "localhost")
        port = kwargs.get("port", 6379)
        db = kwargs.get("db", 0)
        username = kwargs.get("username")
        password = kwargs.get("password")
        scheme = "rediss" if is_ssl else "redis"

        auth = ""
        if username and password:
            auth = f"{username}:{password}@"
        elif password:
            auth = f":{password}@"
        elif username:
            auth = f"{username}@"

        url = f"{scheme}://{auth}{host}:{port}/{db}"
        return syncredis.from_url(url, decode_responses=True)
    except Exception:
        return syncredis.from_url(settings.redis_url, decode_responses=True)


def invalidate_services_cache_sync(
    service_id: Optional[int] = None,
    redis: Optional[aioredis.Redis] = None
) -> int:
    """
    Synchronous wrapper for invalidate_services_cache.
    Uses a dedicated synchronous Redis client to safely delete keys without loop conflicts.
    Safe to call from background workers, threads, or sync functions (e.g. hold sweeper).
    """
    try:
        sync_client = _get_sync_redis_client(redis)
        total_deleted = 0
        if service_id is not None:
            direct_keys = [
                make_service_detail_cache_key(service_id),
                make_service_seats_cache_key(service_id),
            ]
            total_deleted += sync_client.delete(*direct_keys)
            list_keys = list(sync_client.scan_iter(match=f"{CACHE_SERVICES_PREFIX}:list:*", count=100))
            if list_keys:
                total_deleted += sync_client.delete(*list_keys)
        else:
            all_keys = list(sync_client.scan_iter(match=f"{CACHE_SERVICES_PREFIX}:*", count=100))
            if all_keys:
                total_deleted += sync_client.delete(*all_keys)
        sync_client.close()
        return total_deleted
    except Exception as e:
        logger.warning(f"Sync invalidate_services_cache failed: {e}")
        return 0
