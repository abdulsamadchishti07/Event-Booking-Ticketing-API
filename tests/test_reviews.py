from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.orm import Session

from app import oauth2, utils
from app.database import model


@pytest.fixture
def other_user(db_session: Session) -> model.User:
    """Creates a second customer user for authorization testing."""
    user = model.User(
        name="Other Customer",
        email="other_customer@example.com",
        password_hash=utils.hash("Password123!"),
        dob=date(1995, 1, 1),
        role=model.UserRole.CUSTOMER,
        phone_no="+1000000099",
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def other_headers(other_user: model.User) -> dict[str, str]:
    token = oauth2.create_access_token(data={"user_id": other_user.id, "sub": str(other_user.id)})
    return {"Authorization": f"Bearer {token}"}


def create_test_event_and_confirmed_booking(
    db_session: Session,
    seller_user: model.User,
    customer_user: model.User,
    is_past_event: bool = True,
    booking_status: model.BookingStatus = model.BookingStatus.CONFIRMED
):
    """Direct database helper to seed an event and a booking with specified parameters."""
    now = datetime.now(timezone.utc)
    if is_past_event:
        start_time = now - timedelta(days=2)
        end_time = now - timedelta(days=1, hours=20)
    else:
        start_time = now + timedelta(days=2)
        end_time = now + timedelta(days=2, hours=4)

    # 1. Location
    location = model.Location(
        seller_id=seller_user.seller_profile.id,
        city="Lahore",
        country="Pakistan",
        address_line="Hall 1"
    )
    db_session.add(location)
    db_session.flush()

    # 2. Service
    service = model.Services(
        seller_id=seller_user.seller_profile.id,
        location_id=location.id,
        service_name="Live Music Festival",
        service_desc="Great live concert",
        booking_mode=model.BookingMode.SLOT_CAPACITY,
        max_capacity=100,
        base_price=Decimal("40.00"),
        start_time=start_time,
        end_time=end_time
    )
    db_session.add(service)
    db_session.flush()

    # 3. Tier
    tier = model.ServiceTier(
        service_id=service.id,
        name="Standard",
        price=Decimal("40.00")
    )
    db_session.add(tier)
    db_session.flush()

    # 4. Booking
    booking = model.Booking(
        user_id=customer_user.id,
        service_id=service.id,
        tier_id=tier.id,
        quantity=1,
        start_time=start_time,
        end_time=end_time,
        status=booking_status
    )
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)
    db_session.refresh(service)

    return service, booking


# ==========================================================
# 1. CREATE REVIEW TESTS
# ==========================================================

async def test_create_review_success(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    service, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    response = await client.post(
        "/reviews",
        headers=user_headers,
        json={
            "booking_id": booking.id,
            "rating": 5,
            "comment": "Incredible performance, loved every minute!"
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert data["booking_id"] == booking.id
    assert data["rating"] == 5
    assert data["comment"] == "Incredible performance, loved every minute!"
    assert "id" in data
    assert "created_at" in data


async def test_create_review_future_event_rejected(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=False
    )

    response = await client.post(
        "/reviews",
        headers=user_headers,
        json={
            "booking_id": booking.id,
            "rating": 5,
            "comment": "Too early to review!"
        }
    )
    assert response.status_code == 400
    assert "Cannot review an event before it has taken place" in response.json()["detail"]


async def test_create_review_unconfirmed_booking_rejected(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True, booking_status=model.BookingStatus.PENDING
    )

    response = await client.post(
        "/reviews",
        headers=user_headers,
        json={
            "booking_id": booking.id,
            "rating": 4,
            "comment": "Good event"
        }
    )
    assert response.status_code == 400
    assert "Only confirmed paid bookings can be reviewed" in response.json()["detail"]


async def test_create_review_not_owner_forbidden(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    other_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    response = await client.post(
        "/reviews",
        headers=other_headers,
        json={
            "booking_id": booking.id,
            "rating": 3,
            "comment": "I didn't book this"
        }
    )
    assert response.status_code == 403
    assert "You are not authorized to review this booking" in response.json()["detail"]


async def test_create_review_duplicate_conflict(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    # First review
    first_res = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 5, "comment": "First review"}
    )
    assert first_res.status_code == 201

    # Second review should fail with 409
    second_res = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 4, "comment": "Duplicate review"}
    )
    assert second_res.status_code == 409
    assert "already reviewed this booking" in second_res.json()["detail"]


async def test_create_review_invalid_rating(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    response = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 6}
    )
    assert response.status_code == 422


# ==========================================================
# 2. GET SERVICE REVIEWS SUMMARY
# ==========================================================

async def test_get_service_reviews_summary(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    other_user: model.User,
    user_headers: dict[str, str],
    other_headers: dict[str, str],
    db_session: Session
):
    service, booking1 = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    # Second booking for other_user on same service
    booking2 = model.Booking(
        user_id=other_user.id,
        service_id=service.id,
        tier_id=service.tiers[0].id,
        quantity=1,
        start_time=service.start_time,
        end_time=service.end_time,
        status=model.BookingStatus.CONFIRMED
    )
    db_session.add(booking2)
    db_session.commit()

    # User 1 posts 5-star review
    res1 = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking1.id, "rating": 5, "comment": "Superb!"}
    )
    assert res1.status_code == 201

    # User 2 posts 4-star review
    res2 = await client.post(
        "/reviews",
        headers=other_headers,
        json={"booking_id": booking2.id, "rating": 4, "comment": "Good sound quality"}
    )
    assert res2.status_code == 201

    # Fetch summary publicly
    summary_res = await client.get(f"/services/{service.id}/reviews")
    assert summary_res.status_code == 200
    data = summary_res.json()
    assert data["service_id"] == service.id
    assert data["total_reviews"] == 2
    assert data["average_rating"] == 4.5
    assert len(data["reviews"]) == 2


async def test_get_service_reviews_empty(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    db_session: Session
):
    service, _ = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    response = await client.get(f"/services/{service.id}/reviews")
    assert response.status_code == 200
    data = response.json()
    assert data["service_id"] == service.id
    assert data["total_reviews"] == 0
    assert data["average_rating"] == 0.0
    assert data["reviews"] == []


async def test_get_service_reviews_not_found(client: httpx.AsyncClient):
    response = await client.get("/services/999999/reviews")
    assert response.status_code == 404


# ==========================================================
# 3. UPDATE REVIEW TESTS
# ==========================================================

async def test_update_review_success(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    create_res = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 3, "comment": "It was ok"}
    )
    assert create_res.status_code == 201
    review_id = create_res.json()["id"]

    # Update rating to 5
    update_res = await client.put(
        f"/reviews/{review_id}",
        headers=user_headers,
        json={"rating": 5, "comment": "Actually changed my mind, it was brilliant!"}
    )
    assert update_res.status_code == 200
    data = update_res.json()
    assert data["id"] == review_id
    assert data["rating"] == 5
    assert data["comment"] == "Actually changed my mind, it was brilliant!"


async def test_update_review_non_owner_forbidden(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    other_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    create_res = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 4, "comment": "Good"}
    )
    review_id = create_res.json()["id"]

    update_res = await client.put(
        f"/reviews/{review_id}",
        headers=other_headers,
        json={"rating": 1, "comment": "Hacked review"}
    )
    assert update_res.status_code == 403


async def test_update_review_admin_authorized(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    admin_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    create_res = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 1, "comment": "Offensive content moderation needed"}
    )
    review_id = create_res.json()["id"]

    # Admin moderates the review comment
    update_res = await client.put(
        f"/reviews/{review_id}",
        headers=admin_headers,
        json={"comment": "[Comment removed by moderator]"}
    )
    assert update_res.status_code == 200
    assert update_res.json()["comment"] == "[Comment removed by moderator]"
    assert update_res.json()["rating"] == 1


# ==========================================================
# 4. DELETE REVIEW TESTS
# ==========================================================

async def test_delete_review_success(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    service, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    create_res = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 5, "comment": "Loved it"}
    )
    review_id = create_res.json()["id"]

    # Delete
    del_res = await client.delete(f"/reviews/{review_id}", headers=user_headers)
    assert del_res.status_code == 204

    # Verify review is gone from summary
    summary_res = await client.get(f"/services/{service.id}/reviews")
    assert summary_res.status_code == 200
    assert summary_res.json()["total_reviews"] == 0


async def test_delete_review_non_owner_forbidden(
    client: httpx.AsyncClient,
    seller_user: model.User,
    test_user: model.User,
    user_headers: dict[str, str],
    other_headers: dict[str, str],
    db_session: Session
):
    _, booking = create_test_event_and_confirmed_booking(
        db_session, seller_user, test_user, is_past_event=True
    )

    create_res = await client.post(
        "/reviews",
        headers=user_headers,
        json={"booking_id": booking.id, "rating": 5, "comment": "Loved it"}
    )
    review_id = create_res.json()["id"]

    del_res = await client.delete(f"/reviews/{review_id}", headers=other_headers)
    assert del_res.status_code == 403
