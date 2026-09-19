import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import AsyncGenerator, Generator

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app import model, oauth2, redis_client, utils
from app.database import engine, get_db
from app.main import app

# Dedicated test Redis database index (DB 15 keeps dev DB 0 completely untouched)
TEST_REDIS_URL = "redis://localhost:6379/15"


@pytest.fixture(autouse=True)
def mock_send_email(monkeypatch):
    """
    Prevents background email tasks from hitting SMTP or external services during tests.
    """
    monkeypatch.setattr("app.email.send_otp_email", lambda to_email, otp: None)
    monkeypatch.setattr("app.email.send_password_reset_email", lambda to_email, otp: None)
    monkeypatch.setattr("app.email.send_mail", lambda to_email, subject, html_content: None)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """
    Provides a transactional database session per test.
    Uses SQLAlchemy savepoints (nested transactions) so that any db.commit()
    called by endpoints or fixtures only commits the savepoint, and everything
    is safely rolled back when the test finishes.
    """
    connection = engine.connect()
    trans = connection.begin()
    session_factory = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = session_factory()
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, transaction):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    yield session

    session.close()
    trans.rollback()
    connection.close()


@pytest_asyncio.fixture(autouse=True)
async def test_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """
    Provides an isolated Redis client connected to DB 15.
    Flushes the test database before and after each test and monkeypatches
    the global redis_client used across routers and oauth2.
    """
    r = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    await r.flushdb()

    orig_client = redis_client.redis_client
    redis_client.redis_client = r

    yield r

    await r.flushdb()
    redis_client.redis_client = orig_client
    await r.aclose()


@pytest_asyncio.fixture
async def client(
    db_session: Session,
    test_redis: aioredis.Redis
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """
    Asynchronous HTTP test client bound to FastAPI application with
    dependency overrides for database and Redis.
    """
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[redis_client.get_redis] = lambda: test_redis

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db_session: Session) -> model.User:
    """
    Creates a standard verified customer user in the test database.
    Attaches plaintext password 'Password123!' as an attribute for tests.
    """
    user = model.User(
        name="Verified Customer",
        email="customer@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1995, 5, 15),
        role=model.UserRole.CUSTOMER,
        phone_no="+1000000001",
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    user.plain_password = "Password123!"
    return user


@pytest.fixture
def unverified_user(db_session: Session) -> model.User:
    """
    Creates an unverified user awaiting email verification OTP.
    """
    user = model.User(
        name="Unverified User",
        email="unverified@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1998, 8, 20),
        role=model.UserRole.CUSTOMER,
        phone_no="+1000000002",
        is_verified=False,
        verification_otp="123456",
        otp_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    user.plain_password = "Password123!"
    return user


@pytest.fixture
def seller_user(db_session: Session) -> model.User:
    """
    Creates a verified seller user with an active SellerProfile.
    """
    user = model.User(
        name="Event Seller",
        email="seller@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1990, 1, 1),
        role=model.UserRole.SELLER,
        phone_no="+1000000003",
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    seller_profile = model.SellerProfile(
        user_id=user.id,
        business_name="Acme Events Ltd",
        business_desc="Premium concert and ticketing organizer",
    )
    db_session.add(seller_profile)
    db_session.commit()
    db_session.refresh(user)
    user.plain_password = "Password123!"
    return user


@pytest.fixture
def admin_user(db_session: Session) -> model.User:
    """
    Creates a verified system administrator user.
    """
    user = model.User(
        name="System Admin",
        email="admin@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1985, 3, 10),
        role=model.UserRole.ADMIN,
        phone_no="+1000000004",
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    user.plain_password = "Password123!"
    return user


@pytest.fixture
def user_headers(test_user: model.User) -> dict[str, str]:
    token = oauth2.create_access_token(data={"user_id": test_user.id, "sub": str(test_user.id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def unverified_headers(unverified_user: model.User) -> dict[str, str]:
    token = oauth2.create_access_token(data={"user_id": unverified_user.id, "sub": str(unverified_user.id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def seller_headers(seller_user: model.User) -> dict[str, str]:
    token = oauth2.create_access_token(data={"user_id": seller_user.id, "sub": str(seller_user.id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(admin_user: model.User) -> dict[str, str]:
    token = oauth2.create_access_token(data={"user_id": admin_user.id, "sub": str(admin_user.id)})
    return {"Authorization": f"Bearer {token}"}
