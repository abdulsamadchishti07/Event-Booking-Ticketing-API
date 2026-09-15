from app import email
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
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
    # Find user by email (OAuth2 specification passes email in the 'username' field)
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

    return {"access_token": access_token, "token_type": "bearer"}
