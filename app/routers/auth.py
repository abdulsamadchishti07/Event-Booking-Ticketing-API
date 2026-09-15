from app import email
from typing import Annotated
from jose import JWTError, jwt
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from .. import model, oauth2, schema, utils, redis_client
from ..database import get_db

router = APIRouter(
    tags=["Authentication"]
)


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
    response: Response,
    user_credentials: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Session = Depends(get_db)
):

    email = user_credentials.username.strip().lower()


    # 1. Enforce Email Rate Limit (Max 5 attempts in 20 mins = 1200s)
    await redis_client.check_email_and_otp_rate_limiting(
        email=email,
        action="login",
        max_request=5,
        window_seconds=120
    )



    """
    Authenticate user with OAuth2 password request form (username & password in form body):
    - Validates email (username field) and plaintext password
    - Verifies that the user account has completed email OTP verification
    - Generates and returns a signed JWT access token
    """
    # 2 Find user by email (OAuth2 specification passes email in the 'username' field)
    user = db.query(model.User).filter(model.User.email == user_credentials.username).first()

    # Verify password against hash
    if not user or not utils.verify_password(user_credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Ensure account is verified before granting JWT
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email with the OTP code before logging in."
        )

    # 3. SUCCESSFUL LOGIN: Reset the email counter!
    # Because they entered the correct password, they are the real user, not an attacker.
    await redis_client.redis_client.delete(f"Rate_limit_Email:{email}:login")

    # Generate JWT access token with subject set to user_id as string
    access_token = oauth2.create_access_token(
        data={"user_id": user.id, "sub": str(user.id)}
    )
    # 4. Generate  Access Token
    access_token = oauth2.create_access_token(
        data={"user_id": user.id, "sub" : str(user.id)}
    )
    refresh_token, jti = oauth2.create_fresh_token(
        data={"sub": str(user.id)}
    )

    # Store active session in Redis with 7-day TTL (604,800 sec)
    session_ttl = oauth2.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

    await redis_client.redis_client.setex(
        f"session:{user.id}:{jti}",
        session_ttl,
        "active"
    )
    # Attach Refresh Token into an HttpOnly Cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,               # JavaScript cannot steal this token!
        secure=False,                # Set to True when you deploy with HTTPS
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
    except JWTError:
        raise HTTPException(status_code=401, detail="Refresh token expired or invalid. Please log in again.")
    # 3. Check Redis: Has this session been logged out or expired?
    session_key = f"session:{user_id}:{jti}"
    session_exists = await redis_client.redis_client.get(session_key)
    if not session_exists:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired due to inactivity. Please log in again."
        )
    # 4. User must still exist and be active
    user = db.query(model.User).filter(model.User.id == int(user_id)).first()
    if not user or not user.is_verified:
        raise HTTPException(status_code=401, detail="User account not found or unverified")
    # Because the user was active, reset the 7-day timer in Redis!
    session_ttl = oauth2.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    await redis_client.redis_client.expire(session_key, session_ttl)
    # 6. Issue a fresh Access Token
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
    response: Response
):
    # 1. Read refresh token from cookie
    token = request.cookies.get("refresh_token")
    if token:
        try:
            payload = jwt.decode(token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
            user_id = payload.get("sub")
            jti = payload.get("jti")
            if user_id and jti:
                # Instantly delete the session from Redis!
                await redis_client.redis_client.delete(f"session:{user_id}:{jti}")
        except JWTError:
            pass  # If token is already invalid, just proceed to delete cookie

    # 2. Tell the browser to delete the cookie
    response.delete_cookie(key="refresh_token")

    return {"message": "Logged out successfully"}
