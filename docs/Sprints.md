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

| Sprint | Title                                           |          Status          | Primary Focus                                                     |
| :----: | :---------------------------------------------- | :----------------------: | :---------------------------------------------------------------- |
| **1**  | **Foundations & Relational DB Schema**          |     ✅ **COMPLETED**     | PostgreSQL Schema, ER Diagrams, Alembic Migrations                |
| **2**  | **Auth, Session Management & Security**         | 🟡 **TESTING IN PROGRESS** | JWT, Argon2id, Redis Sessions, Rate Limiting, RBAC & Pytest Suite |
| **3**  | **Event Management & Concurrency-Safe Booking** |      ⏳ **UP NEXT**      | Event/Seat CRUD, `SELECT FOR UPDATE`, Race condition prevention   |
| **4**  | **Stripe Payments & Async Webhooks**            |      ⏳ **PENDING**      | PaymentIntents, Webhook signature verification, Invoices, Refunds |
| **5**  | **Redis Caching & Performance Tuning**          |      ⏳ **PENDING**      | Listing cache, cache invalidation on write, endpoint optimization |
| **6**  | **Docker, Nginx & Stress Load Testing**         |      ⏳ **PENDING**      | Docker Compose, Nginx reverse proxy, Locust/k6 concurrency tests  |

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

**Status**: 🟡 **IN PROGRESS (Core Features 100% Completed — Building Test Suite)**

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

- [ ] **Test Configuration & Fixtures (`tests/conftest.py`)**:
  - Test database engine & session fixtures (clean transaction isolation per test).
  - Test Redis fixture (clean keyspace isolation / mock or dedicated test db index).
  - Async HTTP test client (`httpx.AsyncClient`) configured with FastAPI `app`.
  - Helper fixtures for creating verified customer, seller, and admin users with tokens.
- [ ] **User & Profile Tests (`tests/test_users.py`)**:
  - `POST /account/register` (success, duplicate email conflict, invalid payload).
  - `POST /account/verify-otp` (correct OTP, expired OTP, invalid OTP, rate limit).
  - `POST /account/resend-otp` (rate limit enforcement, verified user rejection).
  - `GET /account/me` (authenticated vs unauthenticated vs unverified).
  - `PUT /account/{id}` & `DELETE /account/{id}` (ownership validation).
- [ ] **Authentication & Session Tests (`tests/test_auth.py`)**:
  - `POST /login` (valid credentials, wrong password, lockout after 5 consecutive failures).
  - `POST /refresh` (successful token rotation, missing cookie, expired session, old token reuse rejection).
  - `POST /logout` (access token blacklist verification, session deletion, cookie cleared).
- [ ] **Password Reset Tests (`tests/test_password_reset.py`)**:
  - `POST /account/forgot-password` (identical response for existent vs non-existent email).
  - `POST /account/reset-password` (successful reset with valid OTP, wrong OTP, expired OTP).
  - Verification that previous sessions (`session:{user_id}:*`) are deleted in Redis upon reset.
- [ ] **RBAC & Seller Onboarding Tests (`tests/test_rbac_seller.py`)**:
  - `POST /account/become-seller` (customer becomes seller, duplicate profile rejection, admin preservation).
  - Role guard verification (`require_seller` and `require_admin` denying customer with 403 Forbidden).
- [ ] **Rate Limiting Tests (`tests/test_rate_limiting.py`)**:
  - IP rate limiter (`429 Too Many Requests`).
  - Email action rate limiter for OTP verification, resend, and login.

#### Learning Goal:

> You must be able to explain the entire Auth lifecycle without notes:
> `Login Request -> Argon2 Verify -> Access Token (with JTI) + Refresh Token (HttpOnly Cookie) -> Redis Session TTL -> True Rotation on /refresh -> Logout JTI Blacklist & Session Deletion -> Account Lockout on 5 failures`.

---

### Sprint 3: Core Booking Logic & Concurrency Control

**Status**: ⏳ **UP NEXT (The Technical Core)**

#### The Challenge:

What happens when **100 users try to book the last remaining VIP seat at the exact same millisecond**?
Without strict concurrency control, you get **race conditions and double bookings** (two people get charged for the same seat).

#### What is to be built:

1. **Organizer / Seller Endpoints (`/services`, `/locations`)**:
   - `POST /services`: Create events with either `slot_capacity` or `unit_assigned` mode.
   - `POST /services/{id}/tiers`: Add pricing tiers.
   - `POST /services/{id}/seats`: Bulk generate seats/units for an event.
2. **Public Event Discovery**:
   - `GET /services`: Filter events by date, location, price, and available capacity.
   - `GET /services/{id}/seats`: View seat map with real-time status (`available` vs `reserved`).
3. **Concurrency-Safe Booking Flow (`POST /bookings`)**:
   - **For `slot_capacity` Events**:
     - Use a PostgreSQL atomic update:  
       `UPDATE services SET booked_count = booked_count + :qty WHERE id = :id AND (max_capacity - booked_count) >= :qty`
   - **For `unit_assigned` Events (Seats)**:
     - Use PostgreSQL **Pessimistic Locking**:  
       `SELECT * FROM inventory_items WHERE id IN (:seat_ids) AND status = 'available' FOR UPDATE`
     - Mark seats as `reserved` with a **10-minute temporary hold**.
     - Alternative / Enhancement: Use a **Redis TTL Lock** (`SET seat:{id}:lock user_id EX 600 NX`) to reserve before hitting DB.
4. **Booking Lifecycle**:
   - `pending` (holding seats for 10 mins awaiting payment).
   - Celery / Background cron or Redis expiration hook to release expired holds if unpaid.

---

### Sprint 4: Stripe Payment Integration & Webhooks

**Status**: ⏳ **PENDING**

#### The Challenge:

Handling asynchronous financial transactions securely. You cannot trust the frontend to say "payment succeeded"; you must rely on server-to-server **Stripe Webhooks** with cryptographic signature verification.

#### What You Will Build:

1. **Stripe PaymentIntent Creation (`POST /bookings/{id}/pay`)**:
   - Calculate amount on the server (never accept price from frontend!).
   - Create Stripe `PaymentIntent` with an **Idempotency Key** to avoid duplicate charges on retries.
   - Return `client_secret` to the client for Stripe checkout.
2. **Stripe Webhook Handler (`POST /webhook/stripe`)**:
   - Read raw request body and verify the `stripe-signature` header using `STRIPE_WEBHOOK_SECRET`.
   - Event `payment_intent.succeeded`:
     - Transition booking from `pending` ➔ `confirmed`.
     - Update seats from `reserved` ➔ `booked`.
     - Automatically generate an `Invoice` record.
   - Event `payment_intent.payment_failed`:
     - Transition booking to `cancelled`.
     - Release reserved seats back to `available`.
3. **Cancellations & Refunds (`POST /bookings/{id}/cancel`)**:
   - Process full or partial refunds via Stripe Refunds API according to cancellation policy.

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

---

## 🎯 Current Immediate Action Items

1. **Sprint 2 Automated Test Suite (`pytest`)**:
   - Create `tests/conftest.py` with test database, Redis client, and test authentication fixtures.
   - Implement test modules: `test_users.py`, `test_auth.py`, `test_password_reset.py`, and `test_rbac_seller.py`.
   - Run `pytest` via `.venv/bin/pytest` and verify 100% green test execution.
2. **Transition to Sprint 3**:
   - Create router `app/routers/services.py` for Event/Service CRUD.
   - Implement the `SELECT FOR UPDATE` locking mechanism for `POST /bookings`.

