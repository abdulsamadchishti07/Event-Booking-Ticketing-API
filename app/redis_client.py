from anyio import current_effective_deadline
import logging
from fastapi import HTTPException, status, Request, Depends

import redis.asyncio as aioredis
from .config import settings


# 1. Global Redis Pool
redis_client = aioredis.from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=10000
)

async def get_redis() -> aioredis.Redis:
    return redis_client

def get_client_ip(request: Request)-> str:
    """
    Safely extracts the real client IP.
    Checks X-Forwarded-For header first (for proxies/Cloudflare),
    then falls back to direct client host.
    """

    forwarded =  request.headers.get("X-Forwarded-For")
    if forwarded:
         return forwarded.split(",")[0].strip()

    if request.client:
        return request.client.host
    
    return "127.0.0.1"
    
class IPRateLimiting():
    
    """
    Atomic rate-limiter using Redis:
    1. Increments the counter for this key.
    2. Sets expiration window on the first request.
    3. Raises HTTP 429 if the count exceeds max_requests.
    """

    # INCR returns the new count after incrementing
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

        # construct key contaning ratelimit
        redis_key = f"Rate_Limit:{ip}:{end_point}"

        # Atomic Increment inside redis
        current_hit = await redis.incr(redis_key)

        # set expiration on the 1st hit
        if current_hit == 1:
            await  redis.expire(redis_key, self.window_seconds)
        
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
    action:str,
    max_request: int,
    window_seconds: int,
    redis: aioredis.Redis = redis_client
): 
    clean_email = email.strip().lower()
    redis_key = f"Rate_limit_Email:{clean_email}:{action}"

    # Attomic Increment
    current_hits = await redis.incr(redis_key)

    # Set expiration on the 1st hit
    if current_hits == 1:
        await  redis.expire(redis_key, window_seconds)

    if current_hits > max_request:
        ttl = await redis.ttl(redis_key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests from {clean_email}. Retry after {ttl} seconds.",
            headers={"Retry-After": str(ttl)}
        )
