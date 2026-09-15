from app import redis_client
from typing import Annotated
import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session

from .. import email, model, oauth2, schema, utils
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
async def verify_otp(
    payload: schema.VerifyOTP,
    db: Session = Depends(get_db)
):

    # Stop 6-digit PIN brute forcing
    await redis_client.check_email_and_otp_rate_limiting(
        email=payload.email,
        action="verify_otp",
        max_request=5,
        window_seconds=300
    )

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
    
    # Once verified successfully, clear the counter:
    await redis_client.redis_client.delete(f"Rate_limit_Email:{payload.email.strip().lower()}:verify_otp")

    return {"message": "Email verified successfully! Your account is now active. You can log in."}


# ==========================================================
# 3. Resend OTP
# ==========================================================
@router.post(
    "/resend-otp",
    response_model=schema.MessageResponse,
    summary="Resend verification OTP"
)
async def resend_otp(
    payload: schema.ResendOTP,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):

    # Prevent email spamming
    await redis_client.check_email_and_otp_rate_limiting(
        email=payload.email,
        action="resend_otp",
        max_request=1,
        window_seconds=120
    )


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


# ==========================================================
# 4. Get Current User Profile (/me)
# ==========================================================
@router.get(
    "/me",
    response_model=schema.UserOut,
    summary="Get current logged-in user profile"
)
def get_me(
    current_user: model.User = Depends(oauth2.get_verified_user)
):
    """
    Returns the profile of the currently authenticated user from the JWT token.
    """
    return current_user


# ==========================================================
# 5. Dynamic User Profile by ID (/{id})
# ==========================================================
@router.get(
    "/{id}",
    response_model=schema.UserOut,
    summary="Get user profile by dynamic ID"
)
def get_user_by_id(
    id: int,
    db: Session = Depends(get_db),
    current_user: model.User = Depends(oauth2.get_verified_user)
):
    """
    Dynamically fetches and returns any user's profile by their user ID:
    e.g. GET /account/1, GET /account/2
    """
    target_user = db.query(model.User).filter(model.User.id == id).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {id} does not exist."
        )

    return target_user



# ==========================================================
# 6. Update User Profile
# ==========================================================
@router.put(
    "/{id}",
    response_model=schema.UserOut,
    summary="Update user profile"
)
def update_user(
    id: int,
    user_update: schema.UserUpdate,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    """
    Updates user details for the authenticated user:
    - Verifies ownership (users can only update their own profile)
    - Checks for email uniqueness if updating email
    - Securely re-hashes password if provided
    """
    user_query = db.query(model.User).filter(model.User.id == id)
    user = user_query.first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with id {id} does not exist."
        )

    # Authorization: Only allow users to update their own profile
    if user.id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not allowed to perform this action."
        )

    # If updating email, ensure it's not already taken by another account
    if user_update.email and user_update.email != user.email:
        email_taken = db.query(model.User).filter(model.User.email == user_update.email).first()
        if email_taken:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Email '{user_update.email}' is already in use by another account."
            )

    # Extract only the fields explicitly provided in the request body
    update_data = user_update.model_dump(exclude_unset=True)

    # Hash new password and map to 'password_hash' column if password was provided
    if "password" in update_data and update_data["password"]:
        update_data["password_hash"] = utils.hash(update_data.pop("password"))

    if update_data:
        user_query.update(update_data, synchronize_session=False)
        db.commit()
        db.refresh(user)

    return user


# ==========================================================
# 7. Delete User Profile
# ==========================================================
@router.delete(
    "/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete user profile"
)
def delete_user(
    id: int,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    """
    Deletes an account:
    - Verifies user exists
    - Ensures user is deleting their own account
    """
    user = db.query(model.User).filter(model.User.id == id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with id {id} does not exist."
        )

    # Authorization: Only allow users to delete their own account
    if user.id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not allowed to perform this action."
        )

    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)