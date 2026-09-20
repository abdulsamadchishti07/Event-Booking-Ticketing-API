import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app import model, utils


# ============================================================================
# Password Reset Tests (POST /account/forgot-password & /account/reset-password)
# ============================================================================

async def test_forgot_password_anti_enumeration(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test anti-enumeration security:
    - Existent and non-existent emails return the EXACT SAME response message.
    - Existent email triggers an OTP key stored in Redis.
    - Non-existent email does not store any OTP in Redis.
    """
    generic_message = "If this email is registered, a password reset code has been sent."

    # 1. Registered user
    resp_existing = await client.post("/account/forgot-password", json={"email": test_user.email})
    assert resp_existing.status_code == 200
    assert resp_existing.json()["message"] == generic_message

    # OTP is stored in Redis
    stored_otp = await test_redis.get(f"reset_pwd_otp:{test_user.email}")
    assert stored_otp is not None
    assert len(stored_otp) == 6

    # 2. Non-existent user
    resp_ghost = await client.post("/account/forgot-password", json={"email": "nonexistent@example.com"})
    assert resp_ghost.status_code == 200
    assert resp_ghost.json()["message"] == generic_message

    # No OTP is stored in Redis for ghost account
    assert await test_redis.get("reset_pwd_otp:nonexistent@example.com") is None


async def test_forgot_password_rate_limiting(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    Test rate limiting on forgot password requests:
    - Max 1 request per 120 seconds.
    - 2nd request returns 429 Too Many Requests.
    """
    resp1 = await client.post("/account/forgot-password", json={"email": test_user.email})
    assert resp1.status_code == 200

    resp2 = await client.post("/account/forgot-password", json={"email": test_user.email})
    assert resp2.status_code == 429
    assert "Too many requests" in resp2.json()["detail"]


async def test_reset_password_success_and_revokes_all_sessions(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis,
    db_session: Session
):
    """
    Test successful password reset:
    - Verifies valid OTP from Redis.
    - Updates password hash in database.
    - Revokes ALL active sessions across devices (session:{user_id}:* in Redis).
    - Cleans up OTP and lockout keys.
    - User can now log in with the new password and old password fails.
    """
    # 1. Simulate active sessions for this user on multiple devices
    await test_redis.set(f"session:{test_user.id}:session_mobile", "active")
    await test_redis.set(f"session:{test_user.id}:session_web", "active")
    assert await test_redis.get(f"session:{test_user.id}:session_mobile") == "active"
    assert await test_redis.get(f"session:{test_user.id}:session_web") == "active"

    # 2. Store OTP in Redis
    otp = "654321"
    await test_redis.set(f"reset_pwd_otp:{test_user.email}", otp)

    # 3. Perform password reset
    new_password = "BrandNewPassword2026!"
    reset_resp = await client.post(
        "/account/reset-password",
        json={
            "email": test_user.email,
            "otp": otp,
            "new_password": new_password
        }
    )
    assert reset_resp.status_code == 200
    assert "Password reset successfully" in reset_resp.json()["message"]

    # 4. Verify all active sessions were revoked in Redis
    assert await test_redis.get(f"session:{test_user.id}:session_mobile") is None
    assert await test_redis.get(f"session:{test_user.id}:session_web") is None

    # 5. Verify OTP was cleared
    assert await test_redis.get(f"reset_pwd_otp:{test_user.email}") is None

    # 6. Verify database password was updated
    db_session.refresh(test_user)
    assert utils.verify_password(new_password, test_user.password_hash)
    assert not utils.verify_password("Password123!", test_user.password_hash)

    # 7. Test login with new password succeeds
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": new_password}
    )
    assert login_resp.status_code == 200
    assert "access_token" in login_resp.json()


async def test_reset_password_wrong_otp(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test reset password failure with incorrect OTP:
    - Returns 400 Bad Request ("Invalid verification code.").
    """
    await test_redis.set(f"reset_pwd_otp:{test_user.email}", "123456")

    response = await client.post(
        "/account/reset-password",
        json={
            "email": test_user.email,
            "otp": "999999",
            "new_password": "NewPassword123!"
        }
    )
    assert response.status_code == 400
    assert "Invalid verification code" in response.json()["detail"]


async def test_reset_password_expired_or_missing_otp(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test reset password when OTP has expired or was not requested:
    - Returns 400 Bad Request.
    """
    response = await client.post(
        "/account/reset-password",
        json={
            "email": test_user.email,
            "otp": "123456",
            "new_password": "NewPassword123!"
        }
    )
    assert response.status_code == 400
    assert "expired or is invalid" in response.json()["detail"]


async def test_reset_password_unlocks_locked_account(
    client: httpx.AsyncClient,
    test_user: model.User,
    test_redis: aioredis.Redis
):
    """
    Test that resetting password clears account lockout:
    - If account was locked due to 5 failed logins, reset password removes the lockout key.
    """
    # Simulate locked account
    await test_redis.set(f"account_locked:{test_user.email}", "locked")
    await test_redis.set(f"failed_logins:{test_user.email}", "5")

    otp = "112233"
    await test_redis.set(f"reset_pwd_otp:{test_user.email}", otp)

    new_password = "UnlockedPassword123!"
    reset_resp = await client.post(
        "/account/reset-password",
        json={
            "email": test_user.email,
            "otp": otp,
            "new_password": new_password
        }
    )
    assert reset_resp.status_code == 200

    # Verify lockout keys were removed
    assert await test_redis.get(f"account_locked:{test_user.email}") is None
    assert await test_redis.get(f"failed_logins:{test_user.email}") is None

    # Verify user can log in immediately
    login_resp = await client.post(
        "/login",
        data={"username": test_user.email, "password": new_password}
    )
    assert login_resp.status_code == 200
