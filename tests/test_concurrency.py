import asyncio
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from app.database import model

# Re-use the setup logic from the bookings tests
from tests.test_bookings import setup_test_event_with_seats

async def test_high_concurrency_booking_race_condition(
    client: httpx.AsyncClient,
    seller_headers: dict[str, str],
    user_headers: dict[str, str],
    db_session
):
    """
    STRESS TEST: Fire 50 simultaneous booking requests for the exact same seat.
    Expected outcome: Exactly 1 succeeds (201). Exactly 49 fail with (409 Conflict).
    """
    # 1. Setup an event and a specific seat
    service_id, tier_id = await setup_test_event_with_seats(client, seller_headers)
    
    seat = db_session.query(model.InventoryItems).filter_by(
        service_id=service_id,
        identifier_code="A-1"
    ).first()

    now = datetime.now(timezone.utc)
    payload = {
        "service_id": service_id,
        "tier_id": tier_id,
        "assigned_unit_ids": [seat.id],
        "quantity": 1,
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=1, hours=2)).isoformat()
    }

    # 2. Fire 50 concurrent requests!
    # We use asyncio.gather to execute them all at the exact same moment
    tasks = []
    for _ in range(50):
        tasks.append(
            client.post("/bookings", headers=user_headers, json=payload)
        )
    
    responses = await asyncio.gather(*tasks)

    # 3. Analyze the results
    status_codes = [res.status_code for res in responses]
    success_count = status_codes.count(201)
    conflict_count = status_codes.count(409)

    # The ultimate proof of concurrency-safety:
    assert success_count == 1, f"Expected exactly 1 success, got {success_count}"
    assert conflict_count == 49, f"Expected exactly 49 conflicts, got {conflict_count}"

    # Verify the seat is indeed marked as RESERVED in the DB
    db_session.refresh(seat)
    assert seat.status == model.ItemStatus.RESERVED
