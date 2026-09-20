from datetime import date, datetime, timedelta, timezone
import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app import model, utils


# ============================================================================
# 1. User Registration Tests (POST /account/register)
# ============================================================================

async def test_register_success(
    client: httpx.AsyncClient,
    db_session: Session
):
    """
    Test successful registration with valid payload:
    - Returns 201 Created.
    - Password is not exposed in response.
    - is_verified is False.
    - User is persisted in database with 6-digit OTP.
    """
    payload = {
        "name": "Jane Doe",
        "email": "janedoe@example.com",
        "password": "SecurePassword123!",
        "dob": "1996-07-20",
        "phone_no": "+1999888777",
        "role": "customer"
    }

    response = await client.post("/account/register", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Jane Doe"
    assert data["email"] == "janedoe@example.com"
    assert data["is_verified"] is False
    assert "password" not in data

    # Verify user in database
    db_user = db_session.query(model.User).filter(model.User.email == "janedoe@example.com").first()
    assert db_user is not None
    assert db_user.verification_otp is not None
    assert len(db_user.verification_otp) == 6
    assert utils.verify_password("SecurePassword123!", db_user.password_hash)


async def test_register_duplicate_email_conflict(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    Test registration conflict when email is already registered:
    - Returns 409 Conflict.
    """
    payload = {
        "name": "Duplicate Person",
        "email": test_user.email,
        "password": "AnotherPassword123!",
        "dob": "1994-01-01",
        "phone_no": "+1000999888"
    }

    response = await client.post("/account/register", json=payload)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


async def test_register_invalid_payload(client: httpx.AsyncClient):
    """
    Test registration with invalid payload:
    - Missing required fields, invalid email format, short password.
    - Returns 422 Unprocessable Entity.
    """
    # Missing password & invalid email
    payload = {
        "name": "Invalid User",
        "email": "not-a-valid-email",
        "dob": "1995-05-15"
    }
    response = await client.post("/account/register", json=payload)
    assert response.status_code == 422


# ============================================================================
# 2. Account Verification Tests (POST /account/verify-otp)
# ============================================================================

async def test_verify_otp_success(
    client: httpx.AsyncClient,
    unverified_user: model.User,
    db_session: Session
):
    """
    Test successful OTP verification:
    - Returns 200 OK.
    - is_verified becomes True.
    - verification_otp and otp_expires_at are cleared.
    """
    payload = {
        "email": unverified_user.email,
        "otp": unverified_user.verification_otp
    }

    response = await client.post("/account/verify-otp", json=payload)
    assert response.status_code == 200
    assert "Email verified successfully" in response.json()["message"]

    # Verify database state
    db_session.refresh(unverified_user)
    assert unverified_user.is_verified is True
    assert unverified_user.verification_otp is None
    assert unverified_user.otp_expires_at is None


async def test_verify_otp_invalid_code(
    client: httpx.AsyncClient,
    unverified_user: model.User
):
    """
    Test OTP verification failure with wrong 6-digit code:
    - Returns 400 Bad Request.
    """
    payload = {
        "email": unverified_user.email,
        "otp": "000000"
    }

    response = await client.post("/account/verify-otp", json=payload)
    assert response.status_code == 400
    assert "Invalid verification code" in response.json()["detail"]


async def test_verify_otp_expired_code(
    client: httpx.AsyncClient,
    unverified_user: model.User,
    db_session: Session
):
    """
    Test OTP verification with an expired code:
    - Returns 400 Bad Request.
    """
    # Set expiration in the past
    unverified_user.otp_expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.commit()

    payload = {
        "email": unverified_user.email,
        "otp": unverified_user.verification_otp
    }

    response = await client.post("/account/verify-otp", json=payload)
    assert response.status_code == 400
    assert "Verification code has expired" in response.json()["detail"]


async def test_verify_otp_already_verified_user(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    Test OTP verification on an account that is already active:
    - Returns 200 OK informing that the account is already verified.
    """
    payload = {
        "email": test_user.email,
        "otp": "123456"
    }

    response = await client.post("/account/verify-otp", json=payload)
    assert response.status_code == 200
    assert "already verified" in response.json()["message"]


async def test_verify_otp_rate_limiting(
    client: httpx.AsyncClient,
    unverified_user: model.User
):
    """
    Test rate limiting on OTP verification attempts (max 5 attempts):
    - 6th attempt returns 429 Too Many Requests.
    """
    payload = {
        "email": unverified_user.email,
        "otp": "000000"
    }

    for _ in range(5):
        resp = await client.post("/account/verify-otp", json=payload)
        assert resp.status_code == 400

    rate_limit_resp = await client.post("/account/verify-otp", json=payload)
    assert rate_limit_resp.status_code == 429
    assert "Too many requests" in rate_limit_resp.json()["detail"]


# ============================================================================
# 3. Resend OTP Tests (POST /account/resend-otp)
# ============================================================================

async def test_resend_otp_success(
    client: httpx.AsyncClient,
    unverified_user: model.User,
    db_session: Session
):
    """
    Test successfully requesting a new OTP:
    - Returns 200 OK.
    - Generates a new OTP with refreshed expiry in database.
    """
    old_otp = unverified_user.verification_otp

    response = await client.post("/account/resend-otp", json={"email": unverified_user.email})
    assert response.status_code == 200
    assert "new verification code has been sent" in response.json()["message"]

    db_session.refresh(unverified_user)
    assert unverified_user.verification_otp is not None
    assert unverified_user.otp_expires_at > datetime.now(timezone.utc)


async def test_resend_otp_rate_limit(
    client: httpx.AsyncClient,
    unverified_user: model.User
):
    """
    Test rate limit on resending OTP (max 1 request per 120s window):
    - 2nd request returns 429 Too Many Requests.
    """
    resp1 = await client.post("/account/resend-otp", json={"email": unverified_user.email})
    assert resp1.status_code == 200

    resp2 = await client.post("/account/resend-otp", json={"email": unverified_user.email})
    assert resp2.status_code == 429
    assert "Too many requests" in resp2.json()["detail"]


async def test_resend_otp_already_verified_user(
    client: httpx.AsyncClient,
    test_user: model.User
):
    """
    Test resend OTP for an already verified user:
    - Returns 200 with notification that account is already verified.
    """
    response = await client.post("/account/resend-otp", json={"email": test_user.email})
    assert response.status_code == 200
    assert "already verified" in response.json()["message"]


# ============================================================================
# 4. Profile Me Endpoint Tests (GET /account/me)
# ============================================================================

async def test_get_me_authenticated(
    client: httpx.AsyncClient,
    test_user: model.User,
    user_headers: dict[str, str]
):
    """
    Test authenticated user fetching their own profile:
    - Returns 200 OK with their profile details.
    """
    response = await client.get("/account/me", headers=user_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == test_user.id
    assert data["email"] == test_user.email
    assert data["name"] == test_user.name


async def test_get_me_unauthenticated(client: httpx.AsyncClient):
    """
    Test unauthenticated access to /me:
    - Returns 401 Unauthorized.
    """
    response = await client.get("/account/me")
    assert response.status_code == 401


async def test_get_me_unverified_account(
    client: httpx.AsyncClient,
    unverified_headers: dict[str, str]
):
    """
    Test unverified user access to /me:
    - Returns 403 Forbidden ("Account is not verified").
    """
    response = await client.get("/account/me", headers=unverified_headers)
    assert response.status_code == 403
    assert "Account is not verified" in response.json()["detail"]


# ============================================================================
# 5. User Update & Delete Tests (PUT /account/{id} & DELETE /account/{id})
# ============================================================================

async def test_update_user_own_profile(
    client: httpx.AsyncClient,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    """
    Test updating own profile details:
    - Returns 200 OK.
    - Updated fields reflected in database.
    """
    update_payload = {
        "name": "Updated Name",
        "phone_no": "+1999777555"
    }

    response = await client.put(
        f"/account/{test_user.id}",
        json=update_payload,
        headers=user_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Name"
    assert data["phone_no"] == "+1999777555"

    db_session.refresh(test_user)
    assert test_user.name == "Updated Name"


async def test_update_user_forbidden_for_other_user(
    client: httpx.AsyncClient,
    seller_user: model.User,
    user_headers: dict[str, str]
):
    """
    Test updating someone else's profile:
    - Returns 403 Forbidden.
    """
    response = await client.put(
        f"/account/{seller_user.id}",
        json={"name": "Hacked Name"},
        headers=user_headers
    )
    assert response.status_code == 403
    assert "not allowed" in response.json()["detail"]


async def test_delete_user_own_profile(
    client: httpx.AsyncClient,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    """
    Test deleting own profile:
    - Returns 204 No Content.
    - User is removed from database.
    """
    response = await client.delete(f"/account/{test_user.id}", headers=user_headers)
    assert response.status_code == 204

    deleted = db_session.query(model.User).filter(model.User.id == test_user.id).first()
    assert deleted is None


async def test_delete_user_forbidden_for_other_user(
    client: httpx.AsyncClient,
    seller_user: model.User,
    user_headers: dict[str, str]
):
    """
    Test deleting someone else's profile:
    - Returns 403 Forbidden.
    """
    response = await client.delete(f"/account/{seller_user.id}", headers=user_headers)
    assert response.status_code == 403
    assert "not allowed" in response.json()["detail"]
