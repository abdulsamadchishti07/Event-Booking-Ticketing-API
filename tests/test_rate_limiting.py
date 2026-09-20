from fastapi import Depends, FastAPI
import httpx
import pytest
import redis.asyncio as aioredis

from app import model, redis_client
from app.main import app


# ============================================================================
# 1. IP Rate Limiting Tests (IPRateLimiting)
# ============================================================================

async def test_ip_rate_limiter_blocks_after_threshold(
    client: httpx.AsyncClient,
    test_redis: aioredis.Redis
):
    """
    Test that IPRateLimiting blocks traffic from a single IP once the limit is exceeded:
    - First 3 requests succeed.
    - 4th request returns 429 Too Many Requests with Retry-After header.
    """
    @app.get("/test-ip-ratelimit", dependencies=[Depends(redis_client.IPRateLimiting(max_requests=3, window_seconds=60))])
    def rate_limited_endpoint():
        return {"status": "ok"}

    for _ in range(3):
        res = await client.get("/test-ip-ratelimit")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

    blocked_res = await client.get("/test-ip-ratelimit")
    assert blocked_res.status_code == 429
    assert "Too many requests" in blocked_res.json()["detail"]
    assert "Retry-After" in blocked_res.headers


async def test_ip_rate_limiter_uses_x_forwarded_for(
    client: httpx.AsyncClient,
    test_redis: aioredis.Redis
):
    """
    Test that IPRateLimiting correctly tracks client IP via X-Forwarded-For:
    - Requests with different forwarded IPs do not share or exhaust each other's quota.
    """
    @app.get("/test-ip-forwarded", dependencies=[Depends(redis_client.IPRateLimiting(max_requests=2, window_seconds=60))])
    def forwarded_route():
        return {"status": "ok"}

    ip_a = "198.51.100.10"
    ip_b = "198.51.100.20"

    # IP A uses both allowed requests
    for _ in range(2):
        res = await client.get("/test-ip-forwarded", headers={"X-Forwarded-For": ip_a})
        assert res.status_code == 200

    # IP A 3rd request is blocked
    res_a_blocked = await client.get("/test-ip-forwarded", headers={"X-Forwarded-For": ip_a})
    assert res_a_blocked.status_code == 429

    # IP B still has quota and succeeds
    res_b_ok = await client.get("/test-ip-forwarded", headers={"X-Forwarded-For": ip_b})
    assert res_b_ok.status_code == 200


# ============================================================================
# 2. Email Action Rate Limiting Tests (check_email_and_otp_rate_limiting)
# ============================================================================

async def test_email_rate_limiting_verify_otp(
    client: httpx.AsyncClient,
    unverified_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test email rate limiting on OTP verification attempts (action='verify_otp'):
    - Max 5 requests per 300 seconds.
    - 6th attempt returns 429 Too Many Requests.
    """
    payload = {"email": unverified_user.email, "otp": "000000"}

    for _ in range(5):
        resp = await client.post("/account/verify-otp", json=payload)
        assert resp.status_code == 400

    limit_resp = await client.post("/account/verify-otp", json=payload)
    assert limit_resp.status_code == 429
    assert f"Too many requests from {unverified_user.email.lower()}" in limit_resp.json()["detail"]


async def test_email_rate_limiting_resend_otp(
    client: httpx.AsyncClient,
    unverified_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test email rate limiting on resend OTP (action='resend_otp'):
    - Max 1 request per 120 seconds.
    - 2nd request returns 429 Too Many Requests.
    """
    resp1 = await client.post("/account/resend-otp", json={"email": unverified_user.email})
    assert resp1.status_code == 200

    resp2 = await client.post("/account/resend-otp", json={"email": unverified_user.email})
    assert resp2.status_code == 429
    assert f"Too many requests from {unverified_user.email.lower()}" in resp2.json()["detail"]


async def test_email_rate_limiting_forgot_password(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test email rate limiting on password reset requests (action='forgot_password'):
    - Max 1 request per 120 seconds.
    - 2nd request returns 429 Too Many Requests.
    """
    resp1 = await client.post("/account/forgot-password", json={"email": test_user.email})
    assert resp1.status_code == 200

    resp2 = await client.post("/account/forgot-password", json={"email": test_user.email})
    assert resp2.status_code == 429
    assert f"Too many requests from {test_user.email.lower()}" in resp2.json()["detail"]


async def test_email_rate_limiting_login(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test email rate limiting on login action:
    - Direct invocation of check_email_and_otp_rate_limiting with action='login'.
    - 6th call raises HTTP 429.
    """
    email = "testrate@example.com"
    for _ in range(5):
        await redis_client.check_email_and_otp_rate_limiting(
            email=email,
            action="login",
            max_request=5,
            window_seconds=120,
            redis=test_redis
        )

    with pytest.raises(Exception) as exc:
        await redis_client.check_email_and_otp_rate_limiting(
            email=email,
            action="login",
            max_request=5,
            window_seconds=120,
            redis=test_redis
        )
    assert exc.value.status_code == 429
    assert "Too many requests" in exc.value.detail
