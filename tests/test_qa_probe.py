"""QA probes written against the real DB/Redis. A FAILING test here == a confirmed bug."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import httpx, pytest, stripe
from sqlalchemy.orm import Session
from app.core.tasks import released_expired_holds
from app.database import model

def iso(d): return d.isoformat()

async def make_service(client, headers, mode="unit_assigned", cap=None):
    loc = await client.post("/locations", headers=headers,
        json={"city": "Lahore", "country": "PK", "address_line": "Hall 1"})
    now = datetime.now(timezone.utc)
    body = {"service_name": "Show", "location_id": loc.json()["id"], "booking_mode": mode,
            "base_price": 25.00, "start_time": iso(now + timedelta(days=1)),
            "end_time": iso(now + timedelta(days=1, hours=3))}
    if cap: body["max_capacity"] = cap
    r = await client.post("/services", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]

# ---- 1. sweeper: Stripe "already succeeded" error text contains the word "canceled" ----
def test_sweeper_must_not_cancel_booking_whose_intent_actually_succeeded(db_session: Session, test_user, seller_user):
    svc_loc = model.Location(seller_id=seller_user.seller_profile.id, city="a", country="b", address_line="c")
    db_session.add(svc_loc); db_session.flush()
    now = datetime.now(timezone.utc)
    svc = model.Services(seller_id=seller_user.seller_profile.id, location_id=svc_loc.id, service_name="x",
        booking_mode=model.BookingMode.SLOT_CAPACITY, max_capacity=10, base_price=10,
        start_time=now + timedelta(days=1), end_time=now + timedelta(days=1, hours=2))
    db_session.add(svc); db_session.flush()
    b = model.Booking(service_id=svc.id, user_id=test_user.id, quantity=1, start_time=svc.start_time,
        end_time=svc.end_time, status=model.BookingStatus.PENDING, created_at=now - timedelta(minutes=30))
    db_session.add(b); db_session.flush()
    db_session.add(model.Payment(booking_id=b.id, stripe_payment_intent_id="pi_real", client_secret="s",
        idempotency_key="k", amount=10, status=model.PaymentStatus.REQUIRES_PAYMENT_METHOD))
    db_session.commit()
    # This is Stripe's real wording when you try to cancel a PI that already succeeded
    err = stripe.InvalidRequestError(
        "You cannot cancel this PaymentIntent because it has a status of succeeded. Only a PaymentIntent "
        "with one of the following statuses may be canceled: requires_payment_method, requires_capture, "
        "requires_confirmation, requires_action, processing.", param=None)
    with patch("stripe.PaymentIntent.cancel", side_effect=err):
        released_expired_holds(db=db_session)
    db_session.refresh(b)
    assert b.status == model.BookingStatus.PENDING, "BUG: paid booking cancelled by sweeper"

# ---- 2. PUT /services with a naive datetime -> naive/aware comparison crash ----
async def test_update_service_naive_datetime_should_not_500(client, seller_headers):
    sid = await make_service(client, seller_headers)
    r = await client.put(f"/services/{sid}", headers=seller_headers,
                         json={"end_time": (datetime.now() + timedelta(days=3)).replace(microsecond=0).isoformat()})
    assert r.status_code < 500

# ---- 3. switching unit_assigned -> slot_capacity without max_capacity breaks booking ----
async def test_switch_mode_without_capacity_should_be_rejected(client, seller_headers, user_headers):
    sid = await make_service(client, seller_headers, mode="unit_assigned")
    r = await client.put(f"/services/{sid}", headers=seller_headers, json={"booking_mode": "slot_capacity"})
    if r.status_code == 200:
        b = await client.post("/bookings", headers=user_headers, json={"service_id": sid, "quantity": 1})
        assert b.status_code < 500, "BUG: service now slot_capacity with max_capacity=NULL"

# ---- 4. PUT /reviews with explicit null rating ----
async def test_review_update_null_rating_should_be_422(client, db_session, test_user, user_headers, seller_user):
    now = datetime.now(timezone.utc)
    loc = model.Location(seller_id=seller_user.seller_profile.id, city="a", country="b", address_line="c")
    db_session.add(loc); db_session.flush()
    svc = model.Services(seller_id=seller_user.seller_profile.id, location_id=loc.id, service_name="x",
        booking_mode=model.BookingMode.SLOT_CAPACITY, max_capacity=5, base_price=1,
        start_time=now - timedelta(days=2), end_time=now - timedelta(days=1))
    db_session.add(svc); db_session.flush()
    bk = model.Booking(service_id=svc.id, user_id=test_user.id, quantity=1, start_time=svc.start_time,
        end_time=svc.end_time, status=model.BookingStatus.CONFIRMED)
    db_session.add(bk); db_session.flush()
    rv = model.Review(booking_id=bk.id, rating=4, comment="ok"); db_session.add(rv); db_session.commit()
    r = await client.put(f"/reviews/{rv.id}", headers=user_headers, json={"rating": None})
    assert r.status_code == 422, f"got {r.status_code}"

# ---- 5. login immediately after password change ----
async def test_login_right_after_password_change_works(client, test_user, user_headers):
    r = await client.put(f"/account/{test_user.id}", headers=user_headers,
        json={"password": "NewPassw0rd!", "current_password": test_user.plain_password})
    assert r.status_code == 200, r.text
    login = await client.post("/login", data={"username": test_user.email, "password": "NewPassw0rd!"})
    assert login.status_code == 200
    tok = login.json()["access_token"]
    me = await client.get("/account/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200, "BUG: brand-new token rejected (revocation cutoff is now+1s)"

# ---- 6. password RESET must kill already-issued access tokens ----
async def test_password_reset_revokes_existing_access_token(client, test_user, user_headers, test_redis):
    await test_redis.set(f"reset_pwd_otp:{test_user.email}", "123456", ex=300)
    r = await client.post("/account/reset-password",
        json={"email": test_user.email, "otp": "123456", "new_password": "BrandNew123!"})
    assert r.status_code == 200, r.text
    me = await client.get("/account/me", headers=user_headers)
    assert me.status_code == 401, "BUG: stolen access token still works after password reset"

# ---- 7. logged-out/revoked refresh token replay wipes the user's OTHER live sessions ----
async def test_stale_refresh_token_must_not_wipe_other_sessions(client, test_user, test_redis):
    a = await client.post("/login", data={"username": test_user.email, "password": test_user.plain_password})
    old_cookie = a.cookies.get("refresh_token")
    await client.post("/logout", headers={"Authorization": f"Bearer {a.json()['access_token']}"},
                      cookies={"refresh_token": old_cookie})
    client.cookies.clear()
    await client.post("/login", data={"username": test_user.email, "password": test_user.plain_password})
    live_before = [k async for k in test_redis.scan_iter(f"session:{test_user.id}:*")]
    client.cookies.clear()
    await client.post("/refresh", cookies={"refresh_token": old_cookie})   # stale token from logged-out session
    live_after = [k async for k in test_redis.scan_iter(f"session:{test_user.id}:*")]
    assert len(live_before) == 1 and len(live_after) == 1, "BUG: stale token wiped a live session"

# ---- 8. email change that only differs by case is stored un-normalised ----
async def test_email_case_only_change_is_normalised(client, test_user, user_headers, db_session):
    await client.put(f"/account/{test_user.id}", headers=user_headers,
        json={"email": "CUSTOMER@example.com", "current_password": test_user.plain_password})
    db_session.refresh(test_user)
    assert test_user.email == test_user.email.lower(), f"stored as {test_user.email!r}"

# ---- 9. login lockout is per-email: attacker can lock a victim out ----
async def test_attacker_cannot_lock_out_victim(client, test_user):
    attacker_headers = {"X-Forwarded-For": "203.0.113.195"}
    for _ in range(5):
        await client.post("/login", data={"username": test_user.email, "password": "wrong-guess"}, headers=attacker_headers)
    # Attacker's IP is locked even if trying correct password
    attacker_try = await client.post("/login", data={"username": test_user.email, "password": test_user.plain_password}, headers=attacker_headers)
    assert attacker_try.status_code == 403, "Attacker IP should be locked"
    # Legitimate victim from different IP is not locked out
    ok = await client.post("/login", data={"username": test_user.email, "password": test_user.plain_password})
    assert ok.status_code == 200, f"victim locked out for 15 min (status {ok.status_code})"

# ---- 10. deleting a seller who has events with bookings ----
async def test_delete_seller_with_booked_event_should_not_500(client, seller_headers, seller_user, user_headers):
    sid = await make_service(client, seller_headers, mode="slot_capacity", cap=5)
    await client.post("/bookings", headers=user_headers, json={"service_id": sid, "quantity": 1})
    r = await client.delete(f"/account/{seller_user.id}", headers=seller_headers)
    assert r.status_code < 500, f"got {r.status_code}"
