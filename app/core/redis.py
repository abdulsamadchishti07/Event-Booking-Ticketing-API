from fastapi import Depends, HTTPException, Request, status
import redis.asyncio as aioredis

from .config import settings

# 1. Global Redis Pool
redis_client = aioredis.from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=30
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
        end_point = request.url.path

        # construct key containing ratelimit
        redis_key = f"Rate_Limit:{ip}:{end_point}"

        # Atomic Increment inside redis
        current_hit = await redis.incr(redis_key)

        # set expiration on the 1st hit
        if current_hit == 1:
            await redis.expire(redis_key, self.window_seconds)
        
        # Block if it hit max hit
        if current_hit > self.max_requests:
            ttl = await redis.ttl(redis_key)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests from {ip}. Retry after {ttl} seconds.",
                headers={"Retry-After": str(ttl)}
            )


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

    # Atomic Increment
    current_hits = await redis.incr(redis_key)

    # Set expiration on the 1st hit
    if current_hits == 1:
        await redis.expire(redis_key, window_seconds)

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
