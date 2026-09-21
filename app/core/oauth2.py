import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.database import get_db, model
from . import redis
from .config import settings

# JWT settings
SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes
REFRESH_TOKEN_EXPIRE_DAYS = settings.refresh_token_expire_days

# OAuth2 scheme: specifies the token retrieval endpoint URL
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="login", auto_error=False)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Creates a signed JWT access token containing the payload data and expiry.
    """
    to_encode = data.copy()

    # Add a unique ID to access tokens for lightweight blacklisting
    if "jti" not in to_encode:
        to_encode["jti"] = str(uuid.uuid4())

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_fresh_token(data: dict) -> tuple[str, str]:
    """
    Creates a long-lived JWT refresh token 7 days.
    Returns both the signed token and its unique session identifier (jti).
    """
    to_encode = data.copy()
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    # We add 'jti' so Redis can track it, and 'type': 'refresh' so it cannot be used as an access token
    to_encode.update({"exp": expire, "jti": jti, "type": "refresh"})
    token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return token, jti


# Blacklist Token
async def add_blacklist_token(token: str) -> None:
    """
    Decodes the access token to get its expiration time ('exp'),
    calculates how many seconds are left, and stores it in Redis.
    Redis will automatically delete the key once the token expires.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        exp = payload.get("exp")
        jti = payload.get("jti")
        if exp and jti:
            now = datetime.now(timezone.utc).timestamp()
            remaining_seconds = int(exp - now)
            if remaining_seconds > 0:
                await redis.redis_client.set(
                    f"blacklist:{jti}",
                    "revoked",
                    ex=remaining_seconds
                )
    except JWTError:
        pass  # If the token is already invalid, no need to blacklist


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)]
) -> model.User:
    """
    Dependency that decodes the JWT bearer token, extracts user ID,
    and returns the authenticated User instance.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"}
    )

    # Decode and verify JWT
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str | None = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Fast lookup by jti:
    if jti and await redis.redis_client.get(f"blacklist:{jti}"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please Login again",
            headers={"WWW-Authenticate": "Bearer"}
        )

    user = db.query(model.User).filter(model.User.id == int(user_id)).first()
    if user is None:
        raise credentials_exception

    return user


async def get_verified_user(
    current_user: Annotated[model.User, Depends(get_current_user)]
) -> model.User:
    """
    Dependency that ensures the authenticated user's email is verified.
    """
    if not current_user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not verified. Please verify your account with OTP."
        )
    return current_user


# Backward compatibility alias for any existing imports
get_verifyied_user = get_verified_user


async def get_optional_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme_optional)],
    db: Annotated[Session, Depends(get_db)],
) -> model.User | None:
    """
    Optional authentication dependency: returns the User if a valid token is provided,
    or None if accessed by a guest.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str | None = payload.get("sub")
        if user_id is None:
            return None
        user = db.query(model.User).filter(model.User.id == int(user_id)).first()
        return user if (user and user.is_verified) else None
    except JWTError:
        return None


# ==========================================================
# Role-Based Access Control (RBAC) Guards
# ==========================================================
class RequireRole:
    """
    FastAPI dependency guard that ensures the current user has one of the allowed roles.
    Usage: current_user = Depends(RequireRole([model.UserRole.SELLER, model.UserRole.ADMIN]))
    """
    def __init__(self, allowed_roles: list[model.UserRole] | list[str]):
        self.allowed_roles = [
            r.value if isinstance(r, model.UserRole) else r for r in allowed_roles
        ]

    def __call__(
        self,
        current_user: Annotated[model.User, Depends(get_verified_user)]
    ) -> model.User:
        user_role = (
            current_user.role.value
            if isinstance(current_user.role, model.UserRole)
            else current_user.role
        )
        if user_role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access forbidden: requires one of the following roles: {', '.join(self.allowed_roles)}"
            )
        return current_user


# Convenience shortcuts for routes
require_seller = RequireRole([model.UserRole.SELLER, model.UserRole.ADMIN])
require_admin = RequireRole([model.UserRole.ADMIN])
