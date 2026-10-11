from typing import Annotated
import jwt
from jwt.exceptions import PyJWTError
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from starlette.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core import oauth2, redis as redis_client
from app.core.config import settings
from app.core import security as utils
from app.database import get_db, model, schema
from app.services import email

router = APIRouter(
    tags=["Authentication"]
)

# Constant-time dummy hash for timing attack mitigation during login
DUMMY_PASSWORD_HASH = utils.hash("timing_attack_mitigation_dummy_password")


@router.post(
    "/login",
    response_model=schema.Token,
    summary="User Login",
    dependencies=[
        Depends(
            redis_client.IPRateLimiting(
                max_requests=100,
                window_seconds=60
            )
        )
    ]
)
async def login(
    request: Request,
    response: Response,
    user_credentials: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Session = Depends(get_db)
):
    """
    Authenticate user with OAuth2 password request form (username & password in form body):
    - Validates email (username field) and plaintext password
    - Verifies that the user account has completed email OTP verification
    - Generates and returns a signed JWT access token and HttpOnly refresh cookie
    """
    email_clean = user_credentials.username.strip().lower()
    client_ip = redis_client.get_client_ip(request)
    lock_key_ip = f"account_locked:{email_clean}:{client_ip}"
    lock_key_email = f"account_locked:{email_clean}"
    failed_key_ip = f"failed_logins:{email_clean}:{client_ip}"
    failed_key_email = f"failed_logins:{email_clean}"

    # 1. Check lock status FIRST before verifying credentials
    if await redis_client.redis_client.get(lock_key_ip):
        ttl = await redis_client.redis_client.ttl(lock_key_ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account is temporarily locked due to multiple failed login attempts. Please try again in {ttl} seconds."
        )
    if await redis_client.redis_client.get(lock_key_email):
        ttl = await redis_client.redis_client.ttl(lock_key_email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account is temporarily locked due to multiple failed login attempts across multiple sources. Please try again in {ttl} seconds."
        )

    # 2. Find user and verify password in constant-time
    user = await run_in_threadpool(lambda: db.query(model.User).filter(func.lower(model.User.email) == email_clean).first())
    password_to_check = user.password_hash if user else DUMMY_PASSWORD_HASH
    is_valid_password = await run_in_threadpool(utils.verify_password, user_credentials.password, password_to_check)

    if not user or not is_valid_password:
        failed_attempts = await redis_client.redis_client.incr(failed_key_ip)
        if failed_attempts == 1:
            await redis_client.redis_client.expire(failed_key_ip, 900)

        failed_email = await redis_client.redis_client.incr(failed_key_email)
        if failed_email == 1:
            await redis_client.redis_client.expire(failed_key_email, 900)

        # 5 consecutive failures from this IP = 15-minute lock for this IP
        if failed_attempts >= 5:
            await redis_client.redis_client.set(lock_key_ip, "locked", ex=900)
            await redis_client.redis_client.delete(failed_key_ip)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been locked for 15 minutes due to 5 consecutive failed login attempts."
            )

        # 25 failures across all sources for this email = 15-minute distributed brute-force lock
        if failed_email >= 25:
            await redis_client.redis_client.set(lock_key_email, "locked", ex=900)
            await redis_client.redis_client.delete(failed_key_email)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been locked for 15 minutes due to excessive failed login attempts across multiple sources."
            )

        remaining = 5 - failed_attempts
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Credentials. {remaining} attempt(s) remaining before account lockout.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Ensure account is verified before granting JWT
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email with the OTP code before logging in."
        )

    # 3. SUCCESSFUL LOGIN: Reset failed counter and lock for this client IP
    await redis_client.redis_client.delete(failed_key_ip)
    await redis_client.redis_client.delete(lock_key_ip)
    await redis_client.redis_client.delete(failed_key_email)


    # 4. Generate  Access Token
    access_token = oauth2.create_access_token(
        data={"user_id": user.id, "sub" : str(user.id)}
    )
    refresh_token, jti = oauth2.create_fresh_token(
        data={"sub": str(user.id)}
    )

    # Store active session in Redis with 7-day TTL (604,800 sec)
    session_ttl = oauth2.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

    await redis_client.redis_client.set(
        f"session:{user.id}:{jti}",
        "active",
        ex=session_ttl
    )
    # Attach Refresh Token into an HttpOnly Cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,               # JavaScript cannot steal this token!
        secure=settings.is_cookie_secure, # Enforces HTTPS in production; configurable in dev
        samesite="lax",              # Protects against CSRF
        max_age=session_ttl          # Cookie will live for 7 days in the browser
    )


    return {"access_token": access_token, "token_type": "bearer"}


@router.post(
    "/refresh",
    response_model=schema.Token,
    summary="Renew Access Token via Refresh Cookie"
)
async def refresh_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db)
):
    # 1. Extract the refresh token from the HttpOnly cookie
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing. Please log in again."
        )
    # 2. Decode and verify JWT
    try:
        payload = jwt.decode(token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
        user_id: str | None = payload.get("sub")
        jti: str | None = payload.get("jti")
        token_type: str | None = payload.get("type")
        if not user_id or not jti or token_type != "refresh":
            raise HTTPException(status_code=401, detail="Invalid refresh token")
    except PyJWTError:
        raise HTTPException(status_code=401, detail="Refresh token expired or invalid. Please log in again.")

    # 3. Check Redis & Rotate: Atomically fetch and delete old session
    old_session_key = f"session:{user_id}:{jti}"
    session_exists = await redis_client.redis_client.getdel(old_session_key)
    if not session_exists:
        delete_cookie_header = "refresh_token=; Max-Age=0; Path=/; SameSite=lax; HttpOnly"
        if settings.is_cookie_secure:
            delete_cookie_header += "; Secure"

        rotated_marker = await redis_client.redis_client.get(f"rotated:{user_id}:{jti}")
        if rotated_marker:
            # Token was previously rotated!
            # Check if this JTI was rotated within the grace window (30 seconds) by a concurrent tab
            grace_session = await redis_client.redis_client.get(f"rotated_grace:{user_id}:{jti}")
            if grace_session:
                # Token was already rotated in another tab within the grace window.
                # Do NOT revoke all sessions! Reject this request without destroying other tabs' sessions.
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session has expired due to inactivity",
                    headers={"Set-Cookie": delete_cookie_header}
                )

            # Replay attack detected outside of grace window! Revoke all sessions for this user!
            async for session_key in redis_client.redis_client.scan_iter(f"session:{user_id}:*"):
                await redis_client.redis_client.delete(session_key)

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has expired due to inactivity",
                headers={"Set-Cookie": delete_cookie_header}
            )

        # Stale token from logout or reset: simply return 401 without wiping other active sessions!
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired due to inactivity",
            headers={"Set-Cookie": delete_cookie_header}
        )

    # 4. User must still exist and be active
    user = await run_in_threadpool(lambda: db.query(model.User).filter(model.User.id == int(user_id)).first())
    if not user or not user.is_verified:
        raise HTTPException(status_code=401, detail="User account not found or unverified")

    # 5. Issue a BRAND NEW Refresh Token + Session (True sliding window!)
    new_fresh_token, new_jti = oauth2.create_fresh_token(
        data={"sub": str(user.id)}
    )
    session_ttl = oauth2.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    await redis_client.redis_client.set(
        f"session:{user.id}:{new_jti}",
        "active",
        ex=session_ttl
    )

    # 6. Store rotation record and grace window (30s)
    await redis_client.redis_client.set(
        f"rotated:{user.id}:{jti}",
        new_jti,
        ex=session_ttl
    )
    await redis_client.redis_client.set(
        f"rotated_grace:{user.id}:{jti}",
        new_jti,
        ex=30
    )

    # 7. Update the HTTP Cookies with the rotated refresh token
    response.set_cookie(
        key="refresh_token",
        value=new_fresh_token,
        httponly=True,
        secure=settings.is_cookie_secure,
        samesite="lax",
        max_age=session_ttl
    )

    # 8. Issue a fresh Access Token
    new_access_token = oauth2.create_access_token(
        data={"user_id": user.id, "sub": str(user.id)}
    )
    return {"access_token": new_access_token, "token_type": "bearer"}


@router.post(
    "/logout",
    summary="Log Out User and Invalidate Session"
)
async def logout(
    request: Request,
    response: Response,
    token: Annotated[str | None, Depends(oauth2.oauth2_scheme_optional)] = None,
):
    refresh_token = request.cookies.get("refresh_token")
    if not token and not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}
        )

    if token:
        if await oauth2.is_token_blacklisted(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked"
            )
        try:
            await oauth2.add_blacklist_token(token)
        except Exception:
            pass

    # Read refresh token from cookie
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        try:
            payload = jwt.decode(refresh_token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
            user_id = payload.get("sub")
            jti = payload.get("jti")
            if user_id and jti:
                # Instantly delete the session from Redis!
                await redis_client.redis_client.delete(f"session:{user_id}:{jti}")
        except PyJWTError:
            pass  # If token is already invalid, just proceed to delete cookie


    # Tell the browser to delete the cookie
    response.delete_cookie(key="refresh_token")

    return {"message": "Logged out successfully"}
