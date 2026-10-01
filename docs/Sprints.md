# 🎟️ Event Booking & Ticketing API — Sprint Roadmap

> **Project Goal**: Build a production-grade, high-concurrency Event Booking & Ticketing REST API.  
> The system enables **Sellers/Organizers** to create venues, events, tiers, and seat inventories, and **Customers** to browse events, reserve seats in real time without race conditions (double-booking), and complete payments via Stripe.

---

## 🧭 Tech Stack & Architecture Overview

| Layer                   | Technologies Used / Planned                                       | Purpose                                                                   |
| :---------------------- | :---------------------------------------------------------------- | :------------------------------------------------------------------------ |
| **Framework**           | **FastAPI** (Python 3.12+ via `uv`)                               | High-performance asynchronous REST API                                    |
| **Database**            | **PostgreSQL** + **SQLAlchemy 2.0** + **Alembic**                 | Relational DB with strict constraints, Mapped ORM, migrations             |
| **Authentication**      | **OAuth2**, **Argon2id** (`pwdlib`), **JWT** (`python-jose`)      | Secure passwords, short-lived Access Tokens, stateless verification       |
| **Cache & Sessions**    | **Redis** (`redis.asyncio`)                                       | Sliding session storage (Refresh Tokens), IP/Email Rate Limiting, caching |
| **Payments**            | **Stripe API** (PaymentIntents, Webhooks)                         | Payment processing, webhook signature verification, idempotency           |
| **Concurrency Control** | **PostgreSQL `SELECT FOR UPDATE`** & **Redis Distributed Locks**  | Guaranteed prevention of double-booking under concurrent traffic          |
| **Deployment & Ops**    | **Docker**, **Nginx** (Reverse Proxy), **Uvicorn** (multi-worker) | Production gateway, load balancing, containerization                      |
| **Benchmarking**        | **Locust** / **k6**                                               | Concurrency stress testing and cache performance measurement              |

---

## 📊 Sprint Tracker

| Sprint | Title                                           |      Status      | Primary Focus                                                     |
| :----: | :---------------------------------------------- | :--------------: | :---------------------------------------------------------------- |
| **1**  | **Foundations & Relational DB Schema**          | ✅ **COMPLETED** | PostgreSQL Schema, ER Diagrams, Alembic Migrations                |
| **2**  | **Auth, Session Management & Security**         | ✅ **COMPLETED** | JWT, Argon2id, Redis Sessions, Rate Limiting, RBAC & Pytest Suite |
| **3**  | **Event Management & Concurrency-Safe Booking** | ✅ **COMPLETED** | Event/Seat CRUD, `SELECT FOR UPDATE`, Race condition prevention   |
| **4**  | **Stripe Payments & Async Webhooks**            | 🟡 **IN PROGRESS** | PaymentIntents, Webhook signature verification, Invoices, Refunds |
| **5**  | **Redis Caching & Performance Tuning**          |  ⏳ **PENDING**  | Listing cache, cache invalidation on write, endpoint optimization |
| **6**  | **Docker, Nginx & Stress Load Testing**         |  ⏳ **PENDING**  | Docker Compose, Nginx reverse proxy, Locust/k6 concurrency tests  |

---

## 📌 Detailed Sprint Breakdown

---

### Sprint 1: Foundations & DB Design

**Status**: ✅ **COMPLETED**

#### What is built:

- Complete database schema designed and migrated using **Alembic** (`6821cd001b18_create_initial_booking_schema.py`).
- Mapped 11 relational entities with proper foreign keys, cascading rules, and check constraints:
  1. `User`: Customers, Sellers, Admins with email verification fields.
  2. `SellerProfile`: Dedicated 1:1 business profile for event organizers.
  3. `Location`: Venues, cities, addresses.
  4. `Services`: Events/shows supporting 2 booking modes:
     - `slot_capacity`: Capacity-based (e.g. 500 General Admission tickets).
     - `unit_assigned`: Specific seat/unit allocation (e.g. Seat A-12).
  5. `ServiceTier`: Pricing tiers (VIP, Regular, Balcony).
  6. `InventoryItems`: Physical or logical seats/units with status (`available`, `reserved`, `booked`, `maintenance`).
  7. `assigns_unit`: M:N association table linking bookings to specific seats.
  8. `Booking`: Reservation records tracking timeframe, tier, quantity, and status (`pending`, `confirmed`, `cancelled`).
  9. `Payment`: Stripe `PaymentIntent` records with idempotency keys.
  10. `Invoice`: Generated invoices upon successful payment.
  11. `Cancellation` & `Review`: Refund tracking and 1–5 star user reviews.

---

### Sprint 2: Authentication, Sessions & Security

**Status**: ✅ **COMPLETED**

#### What is implemented:

- **Password Security**: Argon2id hashing via `pwdlib[argon2,bcrypt]`.
- **User Lifecycle**:
  - Registration (`POST /account/register`) with automatic 6-digit OTP generation.
  - Background email delivery via FastAPI `BackgroundTasks`.
  - OTP Verification (`POST /account/verify-otp`) with 5-minute expiry check.
  - Resend OTP (`POST /account/resend-otp`).
  - **Password Reset Flow**:
    - `POST /account/forgot-password`: Generates 6-digit OTP stored in Redis (5-min TTL) and dispatched via Gmail SMTP. Identical generic response on both user-found and user-not-found paths blocks email enumeration attacks.
    - `POST /account/reset-password`: Verifies OTP, hashes new password with Argon2id, clears lockouts/failed attempts, and **invalidates all active user sessions across all devices** via `revoke_all_user_sessions` (scanning `session:{user_id}:*` in Redis).
- **OAuth2 Login, Session Management & Token Rotation (`app/routers/auth.py`)**:
  - `POST /login`: OAuth2 password form, verifies Argon2id hash & account active status with normalized lowercase email.
  - **Short-lived Access Token**: Signed JWT with unique `jti` (UUID) and 10–15 min expiry returned to client.
  - **Long-lived Refresh Token**: Signed JWT with unique `jti` (UUID) placed in a secure **HttpOnly Cookie**.
  - **Redis Sliding Session**: Active session saved as `session:{user_id}:{jti}` with a 7-day sliding TTL.
  - **True Refresh Token Rotation (`POST /refresh`)**: Invalidates old Redis session, issues a brand-new Refresh Token + JTI with refreshed 7-day TTL, updates the HttpOnly cookie, and issues a new access token.
  - **Instant Revocation on Logout (`POST /logout`)**:
    - Requires active authentication (prevents repeated/unauthorized logouts).
    - Blacklists the Access Token by `jti` in Redis with remaining lifespan TTL (immediately rejecting requests to `/account/me`).
    - Deletes the Refresh Token session in Redis and clears the HttpOnly cookie.
- **Security & Protection (`app/redis_client.py`)**:
  - IP-based rate limiting dependency (`Rate_Limit:{ip}:{path}`).
  - Email action rate limiting (`Rate_limit_Email:{email}:{action}`) preventing OTP & login brute force.
  - **Account Lockout**: Automatically locks account for 15 minutes after 5 consecutive failed login attempts via Redis (`account_locked:{email}`).
  - **Pattern-Based Session Revocation**: `revoke_all_user_sessions(user_id)` helper function using `scan_iter`.
- **Role-Based Access Control (RBAC) (`app/oauth2.py`)**:
  - `RequireRole` dependency guard supporting single/multiple roles (e.g. `[UserRole.SELLER, UserRole.ADMIN]`).
  - Reusable shortcuts: `require_seller`, `require_admin`.
- **Seller Onboarding (`POST /account/become-seller`) (`app/routers/users.py`)**:
  - Validates verified account status, enforces 1:1 `SellerProfile` constraint.
  - Automatically elevates user role to `UserRole.SELLER` (preserving `ADMIN` if already admin).
- **Schema & Database Hardening**:
  - `User.role` converted from raw string to PostgreSQL native enum (`user_role_enum`) with strict typed `UserRole` Enum (`CUSTOMER`, `SELLER`, `ADMIN`).
  - Applied missing database unique constraint on `inventory_items(service_id, identifier_code)` via Alembic migration (`269044107d58`).
- **User Profile Endpoints**:
  - `GET /account/me`, `GET /account/{id}`, `PUT /account/{id}`, `DELETE /account/{id}`.

#### Completed Milestones in Sprint 2:

- [x] **Core Auth & User Lifecycle** (Registration, OTP verification, login, profile CRUD).
- [x] **Redis Session Management & Token Rotation** (HttpOnly refresh cookie, 7-day sliding window).
- [x] **Rate Limiting & Account Lockout** (IP & Email rate limiting, 5-attempt lockout).
- [x] **Password Reset & Security Hardening** (Anti-enumeration, all-session revocation on reset).
- [x] **Role-Based Access Control (RBAC)** (`RequireRole`, `require_seller`, `require_admin`).
- [x] **Seller Onboarding Endpoint** (`POST /account/become-seller`).

#### Automated Test Suite Plan (Pytest):

- [x] **Test Configuration & Fixtures (`tests/conftest.py`)**:
  - Test database engine & session fixtures (clean transaction isolation per test).
  - Test Redis fixture (clean keyspace isolation / mock or dedicated test db index).
  - Async HTTP test client (`httpx.AsyncClient`) configured with FastAPI `app`.
  - Helper fixtures for creating verified customer, seller, and admin users with tokens.
- [x] **User & Profile Tests (`tests/test_users.py`)**:
  - `POST /account/register` (success, duplicate email conflict, invalid payload).
  - `POST /account/verify-otp` (correct OTP, expired OTP, invalid OTP, rate limit).
  - `POST /account/resend-otp` (rate limit enforcement, verified user rejection).
  - `GET /account/me` (authenticated vs unauthenticated vs unverified).
  - `PUT /account/{id}` & `DELETE /account/{id}` (ownership validation).
- [x] **Authentication & Session Tests (`tests/test_auth.py`)**:
  - `POST /login` (valid credentials, wrong password, lockout after 5 consecutive failures).
  - `POST /refresh` (successful token rotation, missing cookie, expired session, old token reuse rejection).
  - `POST /logout` (access token blacklist verification, session deletion, cookie cleared).
- [x] **Password Reset Tests (`tests/test_password_reset.py`)**:
  - `POST /account/forgot-password` (identical response for existent vs non-existent email).
  - `POST /account/reset-password` (successful reset with valid OTP, wrong OTP, expired OTP).
  - Verification that previous sessions (`session:{user_id}:*`) are deleted in Redis upon reset.
- [x] **RBAC & Seller Onboarding Tests (`tests/test_rbac_seller.py`)**:
  - `POST /account/become-seller` (customer becomes seller, duplicate profile rejection, admin preservation).
  - Role guard verification (`require_seller` and `require_admin` denying customer with 403 Forbidden).
- [x] **Rate Limiting Tests (`tests/test_rate_limiting.py`)**:
  - IP rate limiter (`429 Too Many Requests`).
  - Email action rate limiter for OTP verification, resend, and login.

#### Learning Goal:

> You must be able to explain the entire Auth lifecycle without notes:
> `Login Request -> Argon2 Verify -> Access Token (with JTI) + Refresh Token (HttpOnly Cookie) -> Redis Session TTL -> True Rotation on /refresh -> Logout JTI Blacklist & Session Deletion -> Account Lockout on 5 failures`.

---

### Sprint 3: Core Booking Logic & Concurrency Control

**Status**: ✅ **COMPLETED**

#### 🎯 The Core Engineering Challenge:

What happens when **100 concurrent users try to book the last remaining VIP seat at the exact same millisecond**?
Without strict concurrency control and atomic transactions, you get **race conditions and double-bookings** (two users get charged for the exact same seat, causing severe financial and logistical failure).

In this sprint, we build the core business logic of the platform:

1. **Event & Venue Management** for Sellers/Organizers.
2. **Public Event Discovery & Live Seat Maps** for Customers.
3. **High-Concurrency Booking Engine** with PostgreSQL Pessimistic Row Locking (`SELECT FOR UPDATE`) and atomic capacity checks.
4. **Temporary 10-Minute Hold Lifecycle** with automatic hold expiration and inventory release.

---

#### 🏗️ Architecture & Detailed System Workflow:

```
[Customer Request] ──> [Verify JWT & Verified Status]
                             │
                             ▼
         [Select Booking Mode for Service]
           ├── Mode A: 'slot_capacity' (General Admission)
           │     └─ Atomic PostgreSQL UPDATE:
           │        UPDATE services
           │        SET booked_count = booked_count + :qty
           │        WHERE id = :id AND (max_capacity - booked_count) >= :qty
           │
           └── Mode B: 'unit_assigned' (Specific Seats e.g. A-12)
                 └─ Pessimistic Row Lock:
                    SELECT * FROM inventory_items
                    WHERE id IN (:seat_ids) AND service_id = :service_id
                    FOR UPDATE
                    (Freezes rows; concurrent requests queue up)
                             │
                             ▼
            [Validate Status == 'available']
            (If any seat already taken ➔ Rollback & return 409 Conflict)
                             │
                             ▼
            [Transition Seats to 'reserved']
            [Create Booking with status = 'pending']
            [Attach 10-Minute Hold Expiry (UTC)]
                             │
                             ▼
           [Return Booking Summary to Client]
         (Awaiting Stripe Payment in Sprint 4)
```

---

#### What is implemented:

##### 1. Venue & Location Management (`app/routers/locations.py`)

- **`POST /locations`**: Sellers create physical venues (City, Country, Address).
- **`GET /locations`**: Public/seller listing of available locations.
- **`GET /locations/{id}`**: Detailed location info and hosted events.
- **`PUT /locations/{id}` & `DELETE /locations/{id}`**: Seller ownership validation.

##### 2. Event & Service Management (`app/routers/services.py`)

- **`POST /services`**: Sellers create events tied to a location with:
  - `booking_mode`:
    - **`slot_capacity`**: General admission pool (e.g., 500 tickets, no seat picking).
    - **`unit_assigned`**: Seat map allocation (e.g., Row A Seat 1).
  - `base_price`, `max_capacity`, date/time range.
- **`PUT /services/{id}` & `DELETE /services/{id}`**: Restricted to event owner (`seller_id`).
- **`POST /services/{id}/tiers`**: Add pricing tiers (e.g., VIP = $150, Early Bird = $80, Regular = $50).
- **`POST /services/{id}/inventory/bulk`**: Bulk-generate seat inventories (e.g. generating `A-1` through `A-50` assigned to a specific tier).

##### 3. Public Event Discovery & Live Seat Maps (`app/routers/services.py`)

- **`GET /services`**: Public event feed with multi-parameter filtering:
  - City / Country / Location ID.
  - Date range (Upcoming events, weekend events).
  - Price range (Min price to Max price).
  - Booking mode and available capacity filter.
  - Pagination (`limit`, `offset`) and sorting (by date, price).
- **`GET /services/{id}`**: Full event detail including location, seller info, and available pricing tiers.
- **`GET /services/{id}/seats`**: Live visual seat map:
  - Real-time status for every seat (`available`, `reserved`, `booked`, `maintenance`).
  - Tier grouping and pricing.

##### 4. Concurrency-Safe Booking Engine (`app/routers/bookings.py`)

- **`POST /bookings`**:
  - Authenticated customer endpoint (`Depends(oauth2.get_verified_user)`).
  - **Pessimistic Locking Mechanism (`SELECT ... FOR UPDATE`)**:
    - Queries requested seat IDs inside an active database transaction with row-level lock.
    - Ensures no two concurrent transactions can inspect or modify the same seats simultaneously.
    - If any seat is not in `available` state $\rightarrow$ rollback and return `409 Conflict` ("Seat X is no longer available").
  - **10-Minute Hold Creation**:
    - Transitions seats from `available` $\rightarrow$ `reserved`.
    - Inserts `assigns_unit` association records.
    - Inserts `Booking` record with status `pending` and `expires_at = now() + 10 minutes`.
    - Commits transaction atomically.

##### 5. Booking Lifecycle & Automatic Seat Release Engine

- **`GET /bookings/me`**: Customer views their active pending and confirmed bookings.
- **`GET /bookings/{id}`**: Get specific booking detail (with countdown timer for payment).
- **`DELETE /bookings/{id}/release`**: Early customer cancellation of a pending reservation hold (instantly returning seats to `available`).
- **Background Hold Sweeper (`release_expired_holds`)**:
  - Automatically identifies `pending` bookings whose 10-minute hold has elapsed without payment.
  - Transitions `Booking.status` to `cancelled`.
  - Reverts associated `InventoryItems.status` from `reserved` back to `available`.

---

#### 🚦 Sprint 3 Execution Order & Engineering Strategy:

To build this systematically without getting trapped in debugging loops, we follow a strict **hybrid Test-First vs Build-First methodology**:

```
[1. Locations CRUD] ───────────► Write Tests First (TDD) ──► Build Router
[2. Services & Inventory] ─────► Write Tests First (TDD) ──► Build Router
[3. Booking Engine (Locking)] ─► Build Lock Core First ────► Manual Sanity Check ──► Immediate 50-Req Stress Test
[4. Hold Expiration Sweeper] ──► Build Sweeper First ──────► Test with Forced Expired TTL
[5. Public Discovery & Maps] ──► Search, Filter & Live Seat Maps
```

1. **Locations CRUD (`/locations`) — Write the test first (TDD)**:
   - The contract is 100% known upfront: creation returns `201 Created` with expected fields, non-sellers get `403 Forbidden`, and cross-seller ownership violations get `403 Forbidden` on `PUT`/`DELETE`.
   - Writing tests first costs nothing and immediately catches permission and ownership leaks.
2. **Services, Tiers & Bulk Inventory — Write the test first (TDD)**:
   - Same reasoning: schemas and contracts for valid `slot_capacity` vs `unit_assigned` events are clearly defined in the data model. Test-first locks down the validation rules before writing handlers.
3. **Booking Engine (`POST /bookings` with `SELECT FOR UPDATE`) — Build it first**:
   - **Do NOT write the concurrency test before you have written the lock**. You don't yet know your own failure modes: whether the transaction boundary is in the right place, whether you are locking the exact rows needed, or whether rollback properly reverts seat status.
   - **Protocol**: Build the locking logic $\rightarrow$ manually hit it with two sequential requests to verify basic state transitions $\rightarrow$ **immediately write the 50-concurrent-request stress test before moving to any other feature**. That stress test is the centerpiece of your interview story; don't let it slip to "later," because "later" is where concurrency bugs hide.
4. **Hold Expiration Sweeper — Build first, then test**:
   - Sweeper logic is inherently about observing time progression. Build the cleanup sweeper first, then test it by creating a reservation, manually forcing an expired timestamp in the DB, and asserting the sweeper reclaims the seats to `available`.
5. **Public Discovery & Live Seat Map**:
   - Wire up multi-parameter filtering and real-time seat status visualization once the inventory and booking mechanics are battle-tested.

---

#### 📋 Sprint 3 Implementation Milestones:

- [x] **Milestone 3.1 — Locations CRUD (Test-First)**:
  - Write `tests/test_locations.py` (seller-only creation, public listing, 403 on non-owner edit/delete).
  - Implement `app/routers/locations.py` until all location tests pass green.
- [x] **Milestone 3.2 — Services, Tiers & Bulk Inventory (Test-First)**:
  - Write `tests/test_services.py` (`slot_capacity` vs `unit_assigned` creation, tier pricing, bulk seat generation).
  - Implement `app/routers/services.py` seller management endpoints until all service tests pass green.
- [x] **Milestone 3.3 — Concurrency-Safe Booking Core (Build-First)**:
  - Implement `POST /bookings` in `app/routers/bookings.py` using PostgreSQL row-level pessimistic locking (`with_for_update()`).
  - Implement atomic capacity check for `slot_capacity` events.
  - Create 10-minute temporary holds in `pending` status with `reserved` seats.
  - Perform manual 2-request sanity check on state transitions.
- [x] **Milestone 3.4 — High-Concurrency Stress Test Suite (Immediate)**:
  - Write `tests/test_concurrency.py`: Fire 50 simultaneous requests against a single seat using `asyncio.gather` / `ThreadPoolExecutor`.
  - Validate: Exactly 1 request succeeds with `201 Created`; 49 requests receive clean `409 Conflict`. Zero deadlocks, zero double-bookings.
- [x] **Milestone 3.5 — Hold Expiration & Automatic Sweeper (Build-First $\rightarrow$ Test)**:
  - Implement background hold release sweeper function (`release_expired_holds`).
  - Implement manual release endpoint (`DELETE /bookings/{id}/release`).
  - Write lifecycle test: force-expire pending booking $\rightarrow$ run sweeper $\rightarrow$ assert seats return to `available`.
- [x] **Milestone 3.6 — Public Discovery & Real-Time Seat Map**:
  - Implement `GET /services` (filter by city, date, price, capacity).
  - Implement `GET /services/{id}/seats` (live visual status map: `available`, `reserved`, `booked`).

---

#### Automated Test Suite Plan (Pytest) — 88 Tests Passing:

- [x] **Concurrency Double-Booking Stress Test (`tests/test_concurrency.py`)**:
  - Sent 50 simultaneous booking requests for Seat #1 using `asyncio.gather`.
  - **Assertion Verified**: Exactly 1 request receives `201 Created`; the remaining 49 receive `409 Conflict`.
  - **Assertion Verified**: Database contains exactly 1 booking record and Seat #1 status is `reserved`. Zero deadlocks, zero double-bookings.
- [x] **Hold Expiration & Automatic Sweeper Test (`tests/test_booking_lifecycle.py`)**:
  - Reserve seat $\rightarrow$ manually expire timestamp $\rightarrow$ run sweeper $\rightarrow$ assert seat returns to `available` and can be booked by another user.
- [x] **Locations CRUD & Ownership Tests (`tests/test_locations.py`)**:
  - Seller-only creation, public listing, 403 on non-owner edit/delete.
- [x] **Services, Tiers & Inventory Tests (`tests/test_services.py`)**:
  - `slot_capacity` vs `unit_assigned` creation, tier pricing, bulk seat generation.
  - Date filtering (`date_from`, `date_to`), price range filtering (`min_price`, `max_price`), sorting, and bounded pagination (`limit <= 100`).
- [x] **Booking Engine & Permissions Tests (`tests/test_bookings.py`)**:
  - Booking creation with automated date stamping from `Services`.
  - Slot capacity atomic decrements and unit-assigned seat reservations.
  - Role enforcement: Customers cannot create events or locations (`403 Forbidden`); sellers cannot modify other sellers' events.
- [x] **End-to-End Postman Collection & Multi-User Live Verification**:
  - Generated and exported collection in `postman/Event_Booking_Ticketing_API.postman_collection.json`.
  - Tested live against local server with 3 distinct accounts (1 Seller + 2 Customers).
  - Validated live concurrency race condition: Customer 1 books seat (`201 Created`), Customer 2 attempts same seat (`409 Conflict`).

---

#### 💡 Sprint 3 Learning Goal:

> You must be able to explain to an interviewer:
>
> 1. What a **Race Condition** is in booking systems.
> 2. The exact difference between **Pessimistic Locking** (`SELECT FOR UPDATE`) and **Optimistic Locking** (version columns), and why pessimistic locking is necessary for high-contention ticket drops.
> 3. How a **temporary inventory hold lifecycle** prevents overselling without prematurely charging the user before Stripe checkout.

---

### 🏛️ Key Architectural Decision: Event Dates Live on Services, Not Bookings

> **The Problem**: Sprint 1's schema modeled Services the way a generic appointment system (Calendly, Airbnb) would — the service itself is evergreen ("1-Hour Massage"), and the customer picks the time when booking. So `start_time`/`end_time` were placed on `Booking`.
>
> That's wrong for a ticketing platform. An event (a concert, a match) has a fixed date set by the seller, not the customer. Keeping the date on `Booking` caused three real problems:
> 1. **Blind discovery** — `search_services` had no date to filter or sort on, so "show me events this weekend" was impossible to answer.
> 2. **Zombie events** — with no event-level end date, a concert that happened two weeks ago stayed listed and bookable forever.
> 3. **Backwards booking flow** — the customer had to supply `start_time`/`end_time` in the `POST /bookings` payload, meaning they were effectively telling the concert when to happen. For a fixed-date event, that's nonsensical — the date belongs to the event, not the purchase.
>
> **The Fix**: Moved `start_time`/`end_time` onto `Services` (migrated via Alembic `c4c9a1d062db`, indexed for range scans, with a `CheckConstraint` ensuring `end_time > start_time`). `search_services` filters out past events by default and supports `date_from`/`date_to`. `POST /bookings` no longer accepts dates from the client — the backend copies `service.start_time`/`end_time` onto the new `Booking` record automatically.
>
> **Scope Note**: This design assumes one `Services` row = one fixed date/time occurrence. Multiple showtimes for the same event (a movie's 2pm and 8pm screenings) would require a separate `Showtime` entity — explicitly out of scope for now, not an oversight.

---

### Sprint 4: Stripe Payment Integration & Webhooks

**Status**: 🟡 **IN PROGRESS (Financial Settlement Engine)**

#### 🎯 The Core Financial Engineering Challenge:

In Sprint 3, a customer successfully holds a seat or capacity slot for 10 minutes (`Booking.status = "pending"`, `InventoryItems.status = "reserved"`). 
However, **a temporary hold is not a sale**. The core challenge of Sprint 4 is transitioning reservations from temporary holds into irreversible financial and logistical commitments:

1. **Zero-Trust Client Boundary**: The frontend cannot be trusted with monetary amounts or payment confirmations. If a client sends `"amount": 5.00` or claims `"payment succeeded"`, the server must reject it. The backend alone computes the charge, registers the intent with Stripe, and waits for a signed, server-to-server webhook.
2. **Network Failures & Duplicate Charges (Idempotency)**: If a customer double-clicks "Pay" or their mobile connection drops mid-flight, retried requests must never double-charge their card. We enforce this using unique **Stripe Idempotency Keys**.
3. **Cryptographic Webhook Verification**: Webhook endpoints are open to the public internet. Anyone could POST fake JSON claiming a payment succeeded. We must read the raw unparsed request payload and verify Stripe's HMAC-SHA256 signature (`stripe-signature`) against `STRIPE_WEBHOOK_SECRET`.
4. **Asynchronous Settlement State Machine**: The client-side checkout experience is decoupled from backend settlement. The backend must cleanly handle:
   - Success (`payment_intent.succeeded` ➔ `confirmed` + `booked` + `Invoice`).
   - Failure (`payment_intent.payment_failed` ➔ `cancelled` + seats `available`).
   - Webhook retries (handling duplicate webhook deliveries idempotently).

---

#### 🏗️ Architecture & Detailed System Workflow:

```
[Customer with 10-Minute Hold]
             │
             ▼
[POST /bookings/{id}/pay] 
             │
             ├── 1. Validate active hold: (created_at + 10m > now()) & status == 'pending'
             ├── 2. Calculate exact total on backend: (quantity * tier.price)
             ├── 3. Generate unique Idempotency Key (UUID)
             ├── 4. Call Stripe API: stripe.PaymentIntent.create()
             ├── 5. Insert 'payments' row (status='pending', idempotency_key=...)
             └── 6. Return `client_secret` to client for Stripe Elements checkout
                           │
                           ▼
             [Customer Completes Payment on Stripe]
                           │
                           ▼ (Asynchronous Server-to-Server Webhook)
             [POST /webhook/stripe]
                           │
                           ├── 1. Read raw request bytes (request.body())
                           ├── 2. Verify HMAC signature via `stripe.Webhook.construct_event`
                           │      (If signature invalid ➔ 400 Bad Request)
                           │
                           ├── Event: payment_intent.succeeded
                           │     ├── Transition Payment: 'pending' ➔ 'succeeded'
                           │     ├── Transition Booking: 'pending' ➔ 'confirmed'
                           │     ├── Transition Seats:   'reserved' ➔ 'booked'
                           │     └── Auto-generate immutable `invoices` record
                           │
                           └── Event: payment_intent.payment_failed
                                 ├── Transition Payment: 'pending' ➔ 'failed'
                                 ├── Transition Booking: 'pending' ➔ 'cancelled'
                                 └── Release Seats:      'reserved' ➔ 'available'
```

---

#### 📦 What Will Be Built:

##### 1. Stripe SDK Setup & Secret Management (`app/config.py` & `.env`)
- Official `stripe` Python SDK integration.
- Environment variables: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`.

##### 2. Payment Intent Creation (`POST /bookings/{id}/pay`) (`app/routers/payments.py`)
- Authenticated customer endpoint (`get_verified_user`).
- Verifies booking ownership and active 10-minute hold status (`Booking.status == PENDING`).
- Server calculates total amount in smallest currency unit (cents/pennies) based on `service_tier.price` or `service.base_price`.
- Creates Stripe `PaymentIntent` with:
  - `amount`: calculated in cents (integer).
  - `currency`: `"usd"` (or configured currency).
  - `metadata`: `{"booking_id": booking.id, "user_id": current_user.id}`.
  - `idempotency_key`: generated UUID to prevent double charges on network retries.
- Saves initial `Payment` record in PostgreSQL with status `pending`.
- Returns `client_secret` to client.

##### 3. Secure Webhook Handler (`POST /webhook/stripe`) (`app/routers/payments.py`)
- Publicly accessible endpoint receiving raw body payloads directly from Stripe.
- Cryptographically validates the `stripe-signature` header using `stripe.Webhook.construct_event`.
- **Idempotency Guard**: Checks if the `Payment` record was already marked `succeeded` before executing state updates (preventing duplicate processing on webhook retries).
- Dispatches events:
  - `payment_intent.succeeded`:
    - Updates `Payment.status = "succeeded"`.
    - Updates `Booking.status = "confirmed"`.
    - Updates assigned `InventoryItems.status = "booked"`.
    - Automatically creates a new `Invoice` record.
  - `payment_intent.payment_failed`:
    - Updates `Payment.status = "failed"`.
    - Updates `Booking.status = "cancelled"`.
    - Reverts assigned `InventoryItems.status = "available"`.

##### 4. Automatic Invoicing Engine
- Automatically generates an immutable financial record in `invoices` table upon successful payment:
  - `invoice_number`: Unique sequential or formatted string (e.g. `INV-2026-XXXX`).
  - `booking_id`: Linked 1:1 with booking.
  - `subtotal`, `tax_amount`, and `total_amount`.
  - `issued_at`: UTC timestamp.

##### 5. Cancellations & Stripe Refunds (`POST /bookings/{id}/cancel`)
- Customer initiates cancellation of a confirmed booking.
- Validates cancellation policy window (e.g., event has not started yet).
- Calls Stripe Refunds API: `stripe.Refund.create(payment_intent=...)`.
- Reverts seats from `booked` ➔ `available`.
- Inserts audit row in `cancellations` table (`refund_amount`, `reason`, `cancelled_at`).

---

#### 📋 Sprint 4 Implementation Milestones:

- [ ] **Milestone 4.1 — Stripe SDK Setup & Environment Configuration**:
  - Install `stripe` SDK via uv (`uv add stripe`).
  - Configure `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` in `app/config.py` and `.env`.
- [ ] **Milestone 4.2 — Payment Intent Creation (`POST /bookings/{id}/pay`)**:
  - Implement endpoint in `app/routers/payments.py`.
  - Server-calculated pricing, active hold check, idempotency key generation.
  - Insert pending `Payment` row and return `client_secret`.
- [ ] **Milestone 4.3 — Cryptographic Webhook Handler (`POST /webhook/stripe`)**:
  - Read raw `Request.body()`.
  - Verify HMAC signature with `STRIPE_WEBHOOK_SECRET`.
  - Idempotent event dispatcher for `payment_intent.succeeded` & `payment_intent.payment_failed`.
- [ ] **Milestone 4.4 — State Transitions & Invoice Generation**:
  - Transition seats from `reserved` ➔ `booked` on success.
  - Revert seats to `available` on failure.
  - Generate formatted `Invoice` record.
- [ ] **Milestone 4.5 — Cancellations & Stripe Refunds (`POST /bookings/{id}/cancel`)**:
  - Verify cancellation eligibility window.
  - Issue refund via Stripe Refunds API.
  - Release inventory and record in `cancellations`.
- [ ] **Milestone 4.6 — Automated Mocked Pytest Suite (`tests/test_payments.py`)**:
  - Mock Stripe API calls and webhook signature generation for offline, 100% deterministic tests.

---

#### 🧪 Automated Payment & Webhook Test Plan (Pytest):

- **Payment Intent Tests (`tests/test_payments.py`)**:
  - Customer successfully generates `PaymentIntent` for active pending booking (`200 OK` with `client_secret`).
  - Expired hold is rejected when attempting to pay (`400 Bad Request`).
  - Unauthorized customer attempting to pay for another user's booking gets `403 Forbidden`.
  - Paying for already confirmed or cancelled booking gets `400 Bad Request`.
- **Webhook Security & Signature Tests**:
  - Valid signed webhook payload transitions booking to `confirmed` and seats to `booked`.
  - Tampered or missing `stripe-signature` header gets rejected with `400 Bad Request`.
  - Duplicate webhook delivery is handled idempotently without duplicate invoices or errors.
  - `payment_intent.payment_failed` cancels booking and releases seats back to `available`.
- **Refund & Cancellation Tests**:
  - Confirmed booking refund calls Stripe Refund API and creates `cancellations` row.
  - Seats return to `available` and can be immediately re-booked by another customer.

---

#### 💡 Sprint 4 Learning Goal:

> You must be able to explain to an interviewer:
>
> 1. Why **server-to-server Webhooks** are mandatory for financial systems and why you can never trust the frontend for payment confirmation.
> 2. How **HMAC cryptographic signature verification** works to prevent webhook spoofing and replay attacks.
> 3. Why **Idempotency Keys** are required when interacting with payment gateways to prevent double-charging users during network retries.

---

### Sprint 5: Redis Caching & Smart Invalidation

**Status**: ⏳ **PENDING**

#### The Challenge:

Event listings are read 99% of the time and updated 1% of the time. Hitting the database on every browse request destroys performance under traffic.

#### What You Will Build:

1. **Read-Through Event Cache**:
   - Cache `GET /services` queries in Redis (JSON serialized, e.g. key `cache:services:page:1:city:nyc`).
   - Serve responses directly from memory in <5ms.
2. **Cache Invalidation on Write**:
   - When a seller updates an event, deletes a tier, or when seats sell out, invalidate the corresponding cache keys immediately.
3. **Global Rate Limiting & Protection**:
   - Protect high-cost endpoints against DDoS or scraping using Redis token bucket / sliding window counters.

---

### Sprint 6: Production Deployment, Nginx & Stress Load Testing

**Status**: ⏳ **PENDING**

#### The Challenge:

Demonstrating that the system works reliably in production and proving with real data that your concurrency control and caching work.

#### What You Will Build:

1. **Containerization**:
   - Multi-stage `Dockerfile` for FastAPI app.
   - `docker-compose.yml` orchestrating:
     - `web` (FastAPI app)
     - `postgres` (Persistent DB)
     - `redis` (Cache & Session store)
     - `nginx` (Reverse Proxy)
2. **Nginx Reverse Proxy & Load Balancing**:
   - Run Uvicorn with multiple workers (`--workers 4`).
   - Nginx handles TLS termination, gzip compression, and forwards requests to the worker pool.
3. **Load & Concurrency Stress Testing (Locust / k6)**:
   - **Test 1: Concurrency Double-Booking Test**:
     - Fire 50 concurrent requests trying to book the exact same seat.
     - **Verification Goal**: Exactly 1 request receives `201 Created`; 49 receive `409 Conflict`. Zero double-bookings.
   - **Test 2: Cache Performance Benchmark**:
     - Measure `GET /services` latency under 500 virtual users:
       - Without Redis Cache (e.g. 180ms avg, DB spikes).
       - With Redis Cache (e.g. 8ms avg, DB untouched).
     - Save this real benchmark in `docs/` as proof of high-concurrency engineering.
