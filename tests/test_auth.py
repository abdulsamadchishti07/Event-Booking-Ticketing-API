import httpx
import pytest
import redis.asyncio as aioredis
from jose import jwt

from app import model, oauth2


# ============================================================================
# 1. Login Endpoint Tests (POST /login)
# ============================================================================

async def test_login_success(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test successful login with valid credentials.
    Verifies:
    - 200 OK status code.
    - JSON response contains access_token and bearer token type.
    - HttpOnly refresh_token cookie is attached.
    - Active session is stored in Redis under session:{user_id}:{jti}.
    """
    response = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"].lower() == "bearer"

    # Verify refresh token cookie
    assert "refresh_token" in response.cookies
    refresh_token = response.cookies["refresh_token"]

    # Decode refresh token and check payload
    payload = jwt.decode(refresh_token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
    assert payload.get("sub") == str(test_user.id)
    assert payload.get("type") == "refresh"
    jti = payload.get("jti")
    assert jti is not None

    # Verify session in Redis
    session_status = await test_redis.get(f"session:{test_user.id}:{jti}")
    assert session_status == "active"


async def test_login_wrong_password(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test login failure with wrong password.
    Verifies:
    - 401 Unauthorized status code.
    - Remaining attempts counter in error detail.
    - Redis failed_logins counter is incremented.
    """
    response = await client.post(
        "/login",
        data={"username": test_user.email, "password": "WrongPassword!"}
    )

    assert response.status_code == 401
    assert "Invalid Credentials" in response.json()["detail"]
    assert "4 attempt(s) remaining" in response.json()["detail"]
    assert response.headers.get("www-authenticate") == "Bearer"

    # Verify failed logins counter in Redis
    failed_count = await test_redis.get(f"failed_logins:{test_user.email}")
    assert failed_count == "1"


async def test_login_nonexistent_user(
    client: httpx.AsyncClient,
    test_redis: aioredis.Redis
):
    """
    Test login failure with non-registered email.
    Verifies:
    - 401 Unauthorized status code.
    """
    response = await client.post(
        "/login",
        data={"username": "ghost@example.com", "password": "Password123!"}
    )

    assert response.status_code == 401
    assert "Invalid Credentials" in response.json()["detail"]


async def test_login_unverified_account(
    client: httpx.AsyncClient,
    unverified_user: model.User
):
    """
    Test login rejection for accounts that have not verified their OTP.
    Verifies:
    - 403 Forbidden status code.
    - Clear rejection detail message.
    - No refresh cookie attached.
    """
    response = await client.post(
        "/login",
        data={"username": unverified_user.email, "password": unverified_user.plain_password}
    )

    assert response.status_code == 403
    assert "Please verify your email with the OTP code" in response.json()["detail"]
    assert "refresh_token" not in response.cookies


async def test_login_lockout_after_5_failures(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test account lockout protection after 5 consecutive failed login attempts.
    Verifies:
    - Attempts 1-4 return 401 with remaining count.
    - Attempt 5 triggers 403 Forbidden with 15-minute lock notice.
    - Redis account_locked key is set and failed_logins counter is cleared.
    - Attempt 6 with CORRECT credentials is also rejected with 403 while locked.
    """
    # 4 failed attempts
    for i in range(1, 5):
        resp = await client.post(
            "/login",
            data={"username": test_user.email, "password": "WrongPassword!"}
        )
        assert resp.status_code == 401
        assert f"{5 - i} attempt(s) remaining" in resp.json()["detail"]

    # 5th failed attempt -> locks account
    lock_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": "WrongPassword!"}
    )
    assert lock_resp.status_code == 403
    assert "Account has been locked for 15 minutes" in lock_resp.json()["detail"]

    # Check Redis keys
    assert await test_redis.get(f"account_locked:{test_user.email}") == "locked"
    assert await test_redis.get(f"failed_logins:{test_user.email}") is None

    # 6th attempt with CORRECT password must still be blocked by lockout guard
    correct_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert correct_resp.status_code == 403
    assert "temporarily locked due to multiple failed login attempts" in correct_resp.json()["detail"]


async def test_login_clears_failed_attempts_on_success(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test that a successful login resets the failed login counter and email rate limit.
    """
    # 2 failed attempts
    await client.post(
        "/login",
        data={"username": test_user.email, "password": "WrongPassword!"}
    )
    await client.post(
        "/login",
        data={"username": test_user.email, "password": "WrongPassword!"}
    )
    assert await test_redis.get(f"failed_logins:{test_user.email}") == "2"

    # Successful login
    success_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert success_resp.status_code == 200

    # Verify failed logins and email rate limit counters were reset
    assert await test_redis.get(f"failed_logins:{test_user.email}") is None
    assert await test_redis.get(f"Rate_limit_Email:{test_user.email}:login") is None


# ============================================================================
# 2. Token Refresh Endpoint Tests (POST /refresh)
# ============================================================================

async def test_refresh_token_success(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test true sliding window refresh token rotation.
    Verifies:
    - 200 OK status code.
    - New access token issued.
    - Old refresh token cookie rotated to a new refresh token cookie with new JTI.
    - Old session key in Redis is deleted.
    - New session key in Redis is created with active status.
    """
    # 1. Login to obtain initial cookies
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert login_resp.status_code == 200
    old_refresh_token = login_resp.cookies["refresh_token"]
    old_payload = jwt.decode(old_refresh_token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
    old_jti = old_payload["jti"]

    assert await test_redis.get(f"session:{test_user.id}:{old_jti}") == "active"

    # 2. Call /refresh with the cookie set on client instance
    client.cookies.set("refresh_token", old_refresh_token)
    refresh_resp = await client.post("/refresh")
    assert refresh_resp.status_code == 200
    data = refresh_resp.json()
    assert "access_token" in data

    # 3. Verify new cookie
    new_refresh_token = refresh_resp.cookies.get("refresh_token")
    assert new_refresh_token is not None
    assert new_refresh_token != old_refresh_token

    new_payload = jwt.decode(new_refresh_token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
    new_jti = new_payload["jti"]
    assert new_jti != old_jti

    # 4. Verify Redis session rotation
    assert await test_redis.get(f"session:{test_user.id}:{old_jti}") is None
    assert await test_redis.get(f"session:{test_user.id}:{new_jti}") == "active"


async def test_refresh_missing_cookie(client: httpx.AsyncClient):
    """
    Test refresh request without refresh_token cookie.
    Verifies:
    - 401 Unauthorized status code.
    - Clear error message.
    """
    response = await client.post("/refresh")
    assert response.status_code == 401
    assert "Refresh token missing" in response.json()["detail"]


async def test_refresh_invalid_or_tampered_jwt(client: httpx.AsyncClient):
    """
    Test refresh request with a forged or invalid JWT string.
    Verifies:
    - 401 Unauthorized status code.
    """
    client.cookies.set("refresh_token", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.invalid.signature")
    response = await client.post("/refresh")
    assert response.status_code == 401
    assert "Refresh token expired or invalid" in response.json()["detail"]


async def test_refresh_wrong_token_type(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    Test refresh request where client passes an access token instead of a refresh token.
    Verifies:
    - 401 Unauthorized status code.
    """
    # Generate an access token (type is not 'refresh')
    access_token = oauth2.create_access_token(data={"sub": str(test_user.id)})

    client.cookies.set("refresh_token", access_token)
    response = await client.post("/refresh")
    assert response.status_code == 401
    assert "Invalid refresh token" in response.json()["detail"]


async def test_refresh_old_token_reuse_rejected(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    Test security against refresh token reuse.
    Once a refresh token is used, its session is deleted.
    Replaying the old refresh token must be rejected.
    """
    # 1. Initial Login
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    token_v1 = login_resp.cookies["refresh_token"]

    # 2. First rotation (valid)
    client.cookies.set("refresh_token", token_v1)
    rot_resp = await client.post("/refresh")
    assert rot_resp.status_code == 200

    # 3. Attempt replay of token_v1
    client.cookies.set("refresh_token", token_v1)
    replay_resp = await client.post("/refresh")
    assert replay_resp.status_code == 401
    assert "Session has expired due to inactivity" in replay_resp.json()["detail"]


async def test_refresh_unverified_or_nonexistent_user(
    client: httpx.AsyncClient,
    test_redis: aioredis.Redis
):
    """
    Test refresh attempt when the user in the token does not exist in the database.
    """
    non_existent_id = 999999
    token, jti = oauth2.create_fresh_token(data={"sub": str(non_existent_id)})

    # Fake active session in Redis
    await test_redis.set(f"session:{non_existent_id}:{jti}", "active")

    client.cookies.set("refresh_token", token)
    response = await client.post("/refresh")
    assert response.status_code == 401
    assert "User account not found or unverified" in response.json()["detail"]


# ============================================================================
# 3. Logout & Token Revocation Tests (POST /logout)
# ============================================================================

async def test_logout_success(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test successful user logout.
    Verifies:
    - 200 OK status code.
    - Access token JTI added to Redis blacklist.
    - Refresh token session deleted from Redis.
    - Refresh cookie deleted in response.
    """
    # 1. Login
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]
    refresh_token = login_resp.cookies["refresh_token"]

    access_payload = jwt.decode(access_token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
    refresh_payload = jwt.decode(refresh_token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])

    access_jti = access_payload["jti"]
    refresh_jti = refresh_payload["jti"]

    # Verify session exists before logout
    assert await test_redis.get(f"session:{test_user.id}:{refresh_jti}") == "active"

    # 2. Logout
    client.cookies.set("refresh_token", refresh_token)
    logout_resp = await client.post(
        "/logout",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert logout_resp.status_code == 200
    assert logout_resp.json() == {"message": "Logged out successfully"}

    # 3. Verify Redis blacklist and session deletion
    assert await test_redis.get(f"blacklist:{access_jti}") == "revoked"
    assert await test_redis.get(f"session:{test_user.id}:{refresh_jti}") is None


async def test_logged_out_access_token_is_revoked(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test that a blacklisted access token is immediately rejected on subsequent requests.
    """
    # 1. Login
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": test_user.plain_password}
    )
    access_token = login_resp.json()["access_token"]
    refresh_token = login_resp.cookies["refresh_token"]

    # 2. Logout
    client.cookies.set("refresh_token", refresh_token)
    await client.post(
        "/logout",
        headers={"Authorization": f"Bearer {access_token}"}
    )

    # 3. Attempt to use revoked access token on /logout or any protected endpoint
    revoked_resp = await client.post(
        "/logout",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert revoked_resp.status_code == 401
    assert "Token has been revoked" in revoked_resp.json()["detail"]


async def test_logout_unauthenticated(client: httpx.AsyncClient):
    """
    Test logout request without Bearer token.
    Verifies:
    - 401 Unauthorized status code.
    """
    response = await client.post("/logout")
    assert response.status_code == 401
    assert "Not authenticated" in response.json()["detail"]
