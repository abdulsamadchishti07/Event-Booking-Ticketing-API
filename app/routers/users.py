import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import email, model, schema, utils
from ..database import get_db

# OTP configuration
OTP_EXPIRY_MINUTES = 5

router = APIRouter(
    prefix="/account",
    tags=["Account"]
)


# ==========================================================
# 1. User Registration
# ==========================================================
@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=schema.UserOut,
    summary="Register a new account"
)
def create_account(
    user: schema.UserCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Register a new user account:
    - Checks for existing email conflicts
    - Hashes the plaintext password
    - Generates a 6-digit verification OTP valid for 5 minutes
    - Saves unverified user to the database
    - Dispatches verification email asynchronously via background tasks
    """
    # Check if a user with this email is already registered
    existing_user = db.query(model.User).filter(model.User.email == user.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{user.email}' already exists."
        )

    # Securely hash user password
    hashed_password = utils.hash(user.password)

    # Generate a random 6-digit OTP code and expiry timestamp (UTC)
    otp = f"{random.randint(100000, 999999)}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)

    # Create new User model instance
    new_user = model.User(
        name=user.name,
        email=user.email,
        dob=user.dob,
        phone_no=user.phone_no,
        role=user.role,
        password_hash=hashed_password,
        is_verified=False,
        verification_otp=otp,
        otp_expires_at=expires_at
    )

    # Persist user in the database
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Send verification email asynchronously in the background
    background_tasks.add_task(email.send_otp_email, new_user.email, otp)

    return new_user


# ==========================================================
# 2. Account Verification (OTP)
# ==========================================================
@router.post(
    "/verify-otp",
    response_model=schema.MessageResponse,
    summary="Verify account email with OTP"
)
def verify_otp(
    payload: schema.VerifyOTP,
    db: Session = Depends(get_db)
):
    """
    Verifies user's email account using the 6-digit OTP:
    - Confirms user exists
    - Validates OTP code against database
    - Checks if OTP has expired
    - Activates account (is_verified = True) and clears OTP fields
    """
    user = db.query(model.User).filter(model.User.email == payload.email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User with this email does not exist."
        )

    # If the user is already verified, inform them immediately
    if user.is_verified:
        return {"message": "Account is already verified and active. You can log in."}

    # Verify that the OTP matches
    if not user.verification_otp or user.verification_otp != payload.otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code."
        )

    # Verify that the OTP has not expired
    if user.otp_expires_at:
        expires_at = user.otp_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Verification code has expired. Please request a new one."
            )

    # Activate account and invalidate used OTP
    user.is_verified = True
    user.verification_otp = None
    user.otp_expires_at = None
    db.commit()

    return {"message": "Email verified successfully! Your account is now active. You can log in."}


# ==========================================================
# 3. Resend OTP
# ==========================================================
@router.post(
    "/resend-otp",
    response_model=schema.MessageResponse,
    summary="Resend verification OTP"
)
def resend_otp(
    payload: schema.ResendOTP,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Generates and emails a new OTP code for unverified accounts:
    - Confirms user exists
    - Confirms user is not already verified
    - Generates new 6-digit code and refreshes 5-minute expiry
    - Commits new OTP to DB and dispatches email
    """
    user = db.query(model.User).filter(model.User.email == payload.email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with email '{payload.email}' does not exist."
        )

    # If already verified, do not send another OTP
    if user.is_verified:
        return {"message": "Account is already verified and active. You can log in."}

    # Generate a new 6-digit OTP with a refreshed 5-minute expiry window
    otp = f"{random.randint(100000, 999999)}"
    user.verification_otp = otp
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    db.commit()

    # Send new OTP in the background
    background_tasks.add_task(email.send_otp_email, user.email, otp)

    return {"message": "A new verification code has been sent to your email."}
