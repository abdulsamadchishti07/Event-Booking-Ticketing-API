import asyncio
from contextlib import asynccontextmanager
import ipaddress
import logging
import uuid

from fastapi import Depends, HTTPException, Request, status
import redis.asyncio as aioredis

from .config import settings

# 1. Global Redis Pool
redis_client = aioredis.from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=200
)


async def get_redis() -> aioredis.Redis:
    return redis_client


import ipaddress
import logging

logger = logging.getLogger(__name__)


def parse_trusted_proxies(proxy_conf: str) -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    hostnames = set()
    networks = []
    for item in proxy_conf.split(","):
        clean_item = item.strip()
        if not clean_item:
            continue
        try:
            if "/" in clean_item:
                networks.append(ipaddress.ip_network(clean_item, strict=False))
            else:
                addr = ipaddress.ip_address(clean_item)
                networks.append(ipaddress.ip_network(f"{addr}/{32 if addr.version == 4 else 128}"))
        except ValueError:
            hostnames.add(clean_item.lower())
    return hostnames, networks


TRUSTED_HOSTNAMES, TRUSTED_NETWORKS = parse_trusted_proxies(settings.trusted_proxies)


def is_trusted_proxy(ip_or_host: str | None) -> bool:
    if not ip_or_host:
        return False
    clean = ip_or_host.strip().lower()
    if clean in TRUSTED_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(clean)
        return any(addr in net for net in TRUSTED_NETWORKS)
    except ValueError:
        return False


def get_client_ip(request: Request) -> str:
    """
    Safely extracts and validates client IP for rate-limiting.
    - If direct connection is not from a trusted proxy, ignores X-Forwarded-For entirely.
    - If direct connection is a trusted proxy, parses X-Forwarded-For chain from right to left,
      validating all IP addresses with ipaddress.ip_address and returning the first untrusted client IP.
    """
    client_host = request.client.host if request.client else None
    if not client_host:
        return "127.0.0.1"

    # Direct client is not a trusted proxy: do not trust forwarding headers
    if not is_trusted_proxy(client_host):
        try:
            return str(ipaddress.ip_address(client_host))
        except ValueError:
            return client_host

    # Direct client is trusted proxy: walk X-Forwarded-For from right to left
    forwarded = request.headers.get("X-Forwarded-For")
    if not forwarded:
        return client_host

    raw_ips = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not raw_ips:
        return client_host

    valid_ips = []
    for ip_str in raw_ips:
        try:
            valid_ips.append(str(ipaddress.ip_address(ip_str)))
        except ValueError:
            continue

    if not valid_ips:
        return client_host

    # Walk from right to left: skip trusted proxies in chain, return the first untrusted IP
    for ip in reversed(valid_ips):
        if not is_trusted_proxy(ip):
            return ip

    # If all IPs in chain are trusted, return the leftmost valid client IP
    return valid_ips[0]


class IPRateLimiting:
    """
    Atomic rate-limiter using Redis:
    1. Increments the counter for this key.
    2. Sets expiration window on the first request.
    3. Raises HTTP 429 if the count exceeds max_requests.
    Fails open gracefully if Redis connection encounters transient pool saturation.
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        
    async def __call__(
        self,
        request: Request,
        redis: aioredis.Redis = Depends(get_redis)
    ):
        ip = get_client_ip(request)
        route = request.scope.get("route")
        end_point = route.path if route else request.url.path

        # construct key containing ratelimit
        redis_key = f"Rate_Limit:{ip}:{end_point}"

        try:
            # Atomic Increment & Expire inside Redis via pipeline
            pipe = redis.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, self.window_seconds, nx=True)
            res = await pipe.execute()
            current_hit = res[0]
            
            # Block if it hit max hit
            if current_hit > self.max_requests:
                ttl = await redis.ttl(redis_key)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Too many requests from {ip}. Retry after {ttl} seconds.",
                    headers={"Retry-After": str(ttl)}
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"IPRateLimiting transient Redis fallback for {ip}: {e}")
            return


async def check_email_and_otp_rate_limiting(
    email: str,
    action: str,
    max_request: int,
    window_seconds: int,
    redis: aioredis.Redis | None = None
): 
    if redis is None:
        redis = redis_client

    clean_email = email.strip().lower()
    redis_key = f"Rate_limit_Email:{clean_email}:{action}"

    # Atomic Increment & Expire inside Redis via pipeline
    pipe = redis.pipeline()
    pipe.incr(redis_key)
    pipe.expire(redis_key, window_seconds, nx=True)
    res = await pipe.execute()
    current_hits = res[0]

    if current_hits > max_request:
        ttl = await redis.ttl(redis_key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests from {clean_email}. Retry after {ttl} seconds.",
            headers={"Retry-After": str(ttl)}
        )


async def revoke_all_user_sessions(
    user_id: int,
    redis: aioredis.Redis | None = None
) -> int:
    """
    Scans and deletes all active session keys (refresh tokens) for a given user.
    Forces all devices to re-login.
    """
    if redis is None:
        redis = redis_client

    session_keys = [k async for k in redis.scan_iter(f"session:{user_id}:*")]
    if session_keys:
        await redis.delete(*session_keys)
    return len(session_keys)


@asynccontextmanager
async def distributed_lock(
    redis: aioredis.Redis,
    lock_key: str,
    expire_seconds: int = 30,
    acquire_timeout: float = 5.0
):
    """
    Asynchronous distributed lock using Redis with atomic token release (Lua script).
    Guarantees only one worker executes the critical section across distributed nodes.
    """
    token = str(uuid.uuid4())
    end_time = asyncio.get_event_loop().time() + acquire_timeout
    acquired = False

    while asyncio.get_event_loop().time() < end_time:
        if await redis.set(f"lock:{lock_key}", token, nx=True, ex=expire_seconds):
            acquired = True
            break
        await asyncio.sleep(0.05)

    if not acquired:
        raise TimeoutError(f"Could not acquire distributed lock for key: {lock_key}")

    try:
        yield token
    finally:
        # Atomic release using Lua script to prevent releasing someone else's lock if expired
        lua_release = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            await redis.eval(lua_release, 1, f"lock:{lock_key}", token)
        except Exception:
            pass
