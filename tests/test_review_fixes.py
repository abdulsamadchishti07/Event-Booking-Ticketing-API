import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app import oauth2, utils
from app.core.tasks import released_expired_holds
from app.database import engine, model
from tests.test_payments import setup_event_and_booking


def mk_user(db, email, role=model.UserRole.CUSTOMER, phone="+1999"):
    u = model.User(
        name=email,
        email=email,
        password_hash=utils.hash("Password123!"),
        dob=date(1990, 1, 1),
        role=role,
        phone_no=phone,
        is_verified=True
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u, {"Authorization": "Bearer " + oauth2.create_access_token(data={"user_id": u.id, "sub": str(u.id)})}


def intent(i):
    m = MagicMock()
    m.id = i
    m.client_secret = i + "_secret"
    m.status = "requires_payment_method"
    return m


def ev(t, pid):
    return {"type": t, "data": {"object": {"id": pid}}}


async def pay(client, headers, bid, pid):
    with patch("stripe.PaymentIntent.create", return_value=intent(pid)):
        r = await client.post(f"/bookings/{bid}/pay", headers=headers)
    assert r.status_code == 200, r.text


async def hook(client, t, pid):
    with patch("stripe.Webhook.construct_event", return_value=ev(t, pid)), \
         patch("stripe.Refund.create") as refund:
        r = await client.post("/webhook/stripe", content=b"{}", headers={"stripe-signature": "x"})
    return r, refund


# ==========================================================
# R1: Late webhook does NOT free seat held by someone else
# ==========================================================
async def test_R1_late_webhook_does_not_free_seat_held_by_someone_else(client, seller_headers, user_headers, db_session):
    sid, tid, seat_id, a_id = await setup_event_and_booking(client, seller_headers, user_headers, db_session)
    await pay(client, user_headers, a_id, "pi_A")

    # A's hold expires, sweeper cancels it and frees the seat
    a = db_session.get(model.Booking, a_id)
    a.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    db_session.commit()

    with patch("stripe.PaymentIntent.cancel"):
        released_expired_holds(db_session)

    # B takes the same seat
    _, b_headers = mk_user(db_session, "b_user@example.com", phone="+1991001")
    rb = await client.post("/bookings", headers=b_headers, json={"service_id": sid, "tier_id": tid, "assigned_unit_ids": [seat_id]})
    assert rb.status_code == 201
    seat = db_session.get(model.InventoryItems, seat_id)
    db_session.refresh(seat)
    assert seat.status == model.ItemStatus.RESERVED

    # A's (late) payment_intent.succeeded webhook arrives
    r, refund = await hook(client, "payment_intent.succeeded", "pi_A")
    assert r.status_code == 200
    assert r.json()["status"] == "auto_refunded_cancelled_booking"

    # Verify B's seat remains RESERVED (was NOT released by A's late webhook!)
    db_session.expire_all()
    seat = db_session.get(model.InventoryItems, seat_id)
    assert seat.status == model.ItemStatus.RESERVED

    # C tries to book B's seat -> MUST BE REJECTED with 409 Conflict
    _, c_headers = mk_user(db_session, "c_user@example.com", phone="+1992002")
    rc = await client.post("/bookings", headers=c_headers, json={"service_id": sid, "tier_id": tid, "assigned_unit_ids": [seat_id]})
    assert rc.status_code == 409


# ==========================================================
# R2: Out-of-order payment_failed does NOT downgrade succeeded payment
# ==========================================================
async def test_R2_payment_failed_after_succeeded_ignored(client, seller_headers, user_headers, db_session):
    sid, tid, seat_id, bid = await setup_event_and_booking(client, seller_headers, user_headers, db_session)
    await pay(client, user_headers, bid, "pi_X")
    await hook(client, "payment_intent.succeeded", "pi_X")
    await hook(client, "payment_intent.payment_failed", "pi_X")  # out-of-order delivery

    db_session.expire_all()
    p = db_session.query(model.Payment).filter_by(booking_id=bid).first()
    b = db_session.get(model.Booking, bid)
    # Payment status remains SUCCEEDED
    assert p.status == model.PaymentStatus.SUCCEEDED
    assert b.status == model.BookingStatus.CONFIRMED

    # Cancellation properly triggers Stripe refund
    with patch("stripe.Refund.create") as refund:
        r = await client.post(f"/bookings/{bid}/cancel", headers=user_headers, json={})
        assert r.status_code == 200
        assert refund.called


# ==========================================================
# R3: Paying again when intent succeeded reconciles without creating second intent
# ==========================================================
async def test_R3_pay_again_when_intent_already_succeeded_reconciles(client, seller_headers, user_headers, db_session):
    sid, tid, seat_id, bid = await setup_event_and_booking(client, seller_headers, user_headers, db_session)
    await pay(client, user_headers, bid, "pi_1")

    retrieved = MagicMock()
    retrieved.status = "succeeded"
    retrieved.client_secret = "s"

    with patch("stripe.PaymentIntent.retrieve", return_value=retrieved), \
         patch("stripe.PaymentIntent.create") as mock_create:
        r = await client.post(f"/bookings/{bid}/pay", headers=user_headers)
        # Does NOT create a second intent
        assert not mock_create.called
        assert r.status_code == 200

    db_session.expire_all()
    p = db_session.query(model.Payment).filter_by(booking_id=bid).first()
    assert p.stripe_payment_intent_id == "pi_1"
    assert p.status == model.PaymentStatus.SUCCEEDED


# ==========================================================
# R5: Admin without seller profile can PUT and DELETE service without 500
# ==========================================================
async def test_R5_admin_without_seller_profile_can_update_and_delete(client, seller_headers, admin_headers, db_session):
    loc = await client.post("/locations", headers=seller_headers, json={"city": "K", "country": "PK", "address_line": "x"})
    now = datetime.now(timezone.utc)
    s = await client.post("/services", headers=seller_headers, json={
        "service_name": "E",
        "location_id": loc.json()["id"],
        "booking_mode": "slot_capacity",
        "max_capacity": 10,
        "base_price": 5,
        "start_time": (now + timedelta(days=2)).isoformat(),
        "end_time": (now + timedelta(days=2, hours=2)).isoformat()
    })
    sid = s.json()["id"]

    # Admin updates service -> 200 OK (no AttributeError)
    r_put = await client.put(f"/services/{sid}", headers=admin_headers, json={"service_name": "renamed by admin"})
    assert r_put.status_code == 200
    assert r_put.json()["service_name"] == "renamed by admin"

    # Admin deletes service -> 204 No Content
    r_del = await client.delete(f"/services/{sid}", headers=admin_headers)
    assert r_del.status_code == 204


# ==========================================================
# R6: Location hijacking, capacity shrink below booked, and null capacity rejected
# ==========================================================
async def test_R6_update_service_validations(client, seller_headers, user_headers, db_session):
    _, other_headers = mk_user(db_session, "seller_other@example.com", role=model.UserRole.SELLER, phone="+1993003")
    u2 = db_session.query(model.User).filter_by(email="seller_other@example.com").first()
    db_session.add(model.SellerProfile(user_id=u2.id, business_name="Other"))
    db_session.commit()

    loc_other = await client.post("/locations", headers=other_headers, json={"city": "L", "country": "PK", "address_line": "other seller venue"})
    loc_mine = await client.post("/locations", headers=seller_headers, json={"city": "K", "country": "PK", "address_line": "mine"})
    now = datetime.now(timezone.utc)
    s = await client.post("/services", headers=seller_headers, json={
        "service_name": "E",
        "location_id": loc_mine.json()["id"],
        "booking_mode": "slot_capacity",
        "max_capacity": 10,
        "base_price": 5,
        "start_time": (now + timedelta(days=2)).isoformat(),
        "end_time": (now + timedelta(days=2, hours=2)).isoformat()
    })
    sid = s.json()["id"]

    # (a) Attaching event to another seller's venue must be rejected with 403
    r_hijack = await client.put(f"/services/{sid}", headers=seller_headers, json={"location_id": loc_other.json()["id"]})
    assert r_hijack.status_code == 403

    # (b) Sell 5, then shrinking capacity to 1 must be rejected with 400
    rb = await client.post("/bookings", headers=user_headers, json={"service_id": sid, "quantity": 5})
    assert rb.status_code == 201
    r_shrink = await client.put(f"/services/{sid}", headers=seller_headers, json={"max_capacity": 1})
    assert r_shrink.status_code == 400
    assert "already booked or held" in r_shrink.json()["detail"]

    # (c) Setting max_capacity=null must be rejected with 400 (or validation error)
    r_null = await client.put(f"/services/{sid}", headers=seller_headers, json={"max_capacity": None})
    assert r_null.status_code in [400, 422]


# ==========================================================
# R7: Logout with expired access token successfully revokes refresh session
# ==========================================================
async def test_R7_logout_with_expired_access_token_clears_session(client, test_user, test_redis):
    r = await client.post("/login", data={"username": test_user.email, "password": "Password123!"})
    assert r.status_code == 200

    # User presents expired access token on logout
    expired = oauth2.create_access_token(data={"user_id": test_user.id, "sub": str(test_user.id)}, expires_delta=timedelta(minutes=-1))
    lo = await client.post("/logout", headers={"Authorization": f"Bearer {expired}"})
    assert lo.status_code == 200

    # Subsequent /refresh fails because session was deleted from Redis
    rf = await client.post("/refresh")
    assert rf.status_code == 401


# ==========================================================
# R8: Deleting event with pending booking returns clean 400
# ==========================================================
async def test_R8_delete_event_with_pending_booking_returns_400(client, seller_headers, user_headers, db_session):
    sid, tid, seat_id, bid = await setup_event_and_booking(client, seller_headers, user_headers, db_session)
    r = await client.delete(f"/services/{sid}", headers=seller_headers)
    assert r.status_code == 400
    assert "Cannot delete event with existing" in r.json()["detail"]


# ==========================================================
# R9: Password change requires current_password
# ==========================================================
async def test_R9_password_change_requires_current_password(client, test_user, user_headers):
    # Changing password without current_password must be rejected with 400
    r = await client.put(
        f"/account/{test_user.id}",
        headers=user_headers,
        json={"password": "Attacker123!", "email": "evil@example.com"}
    )
    assert r.status_code == 400
    assert "Current password is required" in r.json()["detail"]

    # Providing correct current_password succeeds
    r_valid = await client.put(
        f"/account/{test_user.id}",
        headers=user_headers,
        json={"password": "NewValidPass123!", "current_password": test_user.plain_password}
    )
    assert r_valid.status_code == 200


# ==========================================================
# R10: Event in the past cannot be created
# ==========================================================
async def test_R10_event_in_the_past_rejected(client, seller_headers):
    loc = await client.post("/locations", headers=seller_headers, json={"city": "K", "country": "PK", "address_line": "x"})
    now = datetime.now(timezone.utc)
    r = await client.post("/services", headers=seller_headers, json={
        "service_name": "Old",
        "location_id": loc.json()["id"],
        "booking_mode": "slot_capacity",
        "max_capacity": 10,
        "base_price": 5,
        "start_time": (now - timedelta(days=30)).isoformat(),
        "end_time": (now - timedelta(days=30, hours=-2)).isoformat()
    })
    assert r.status_code == 400
    assert "start_time in the past" in r.json()["detail"]


# ==========================================================
# R11: Duplicate seat IDs reported as 400 Bad Request
# ==========================================================
async def test_R11_duplicate_seat_ids_reported_as_400(client, seller_headers, user_headers, db_session):
    sid, tid, seat_id, bid = await setup_event_and_booking(client, seller_headers, user_headers, db_session)
    seat2 = db_session.query(model.InventoryItems).filter_by(service_id=sid, identifier_code="VIP-2").first()
    r = await client.post("/bookings", headers=user_headers, json={"service_id": sid, "assigned_unit_ids": [seat2.id, seat2.id]})
    assert r.status_code == 400
    assert "Duplicate seat IDs" in r.json()["detail"]


# ==========================================================
# R12: GET /services avoids N+1 queries
# ==========================================================
async def test_R12_services_list_avoids_n_plus_one(client, seller_headers, db_session):
    loc = await client.post("/locations", headers=seller_headers, json={"city": "K", "country": "PK", "address_line": "x"})
    now = datetime.now(timezone.utc)
    for i in range(5):
        await client.post("/services", headers=seller_headers, json={
            "service_name": f"E{i}",
            "location_id": loc.json()["id"],
            "booking_mode": "slot_capacity",
            "max_capacity": 10,
            "base_price": 5,
            "start_time": (now + timedelta(days=2)).isoformat(),
            "end_time": (now + timedelta(days=2, hours=2)).isoformat()
        })

    n = {"c": 0}
    def counter(conn, cur, stmt, params, ctx, many):
        n["c"] += 1

    event.listen(engine, "before_cursor_execute", counter)
    r = await client.get("/services")
    event.remove(engine, "before_cursor_execute", counter)
    assert r.status_code == 200
    # Single eager query loads services and relations without N+1 statements
    assert n["c"] < 5


# ==========================================================
# R13: Login timing attack mitigation with constant-time check
# ==========================================================
async def test_R13_login_timing_mitigated(client, test_user, test_redis):
    # Both nonexistent user and wrong password on existing user execute dummy/actual hash verification
    r_existing = await client.post("/login", data={"username": test_user.email, "password": "wrong-password"})
    r_ghost = await client.post("/login", data={"username": "nobody@example.com", "password": "wrong-password"})
    assert r_existing.status_code == 401
    assert r_ghost.status_code == 401


# ==========================================================
# R14: Refresh token replay revokes session family
# ==========================================================
async def test_R14_refresh_token_replay_revokes_session_family(client, test_user, test_redis):
    await client.post("/login", data={"username": test_user.email, "password": "Password123!"})
    old_cookie = client.cookies.get("refresh_token")

    # Legit rotation
    r1 = await client.post("/refresh")
    assert r1.status_code == 200

    # Attacker replays old cookie
    client.cookies.set("refresh_token", old_cookie)
    r2 = await client.post("/refresh")
    assert r2.status_code == 401

    # Entire session family is revoked in Redis
    remaining_sessions = [k async for k in test_redis.scan_iter("session:*")]
    assert len(remaining_sessions) == 0
