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
| **2**  | **Auth, Session Management & Security**         | 🟡 **IN PROGRESS (90%)** | JWT, Argon2id, Redis Sessions, Rate Limiting, RBAC                |
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

**Status**: 🟡 **IN PROGRESS (Finishing Up)**

#### What is implemented:

- **Password Security**: Argon2id hashing via `pwdlib[argon2,bcrypt]`.
- **User Lifecycle**:
  - Registration (`POST /account/register`) with automatic 6-digit OTP generation.
  - Background email delivery via FastAPI `BackgroundTasks`.
  - OTP Verification (`POST /account/verify-otp`) with 5-minute expiry check.
  - Resend OTP (`POST /account/resend-otp`).
- **OAuth2 Login & Session Management (`app/routers/auth.py`)**:
  - `POST /login`: OAuth2 password form, verifies Argon2id hash & account active status.
  - **Short-lived Access Token**: JWT with 10–15 min expiry returned to client.
  - **Long-lived Refresh Token**: Signed JWT with unique `jti` (UUID) placed in a secure **HttpOnly Cookie**.
  - **Redis Sliding Session**: Active session saved as `session:{user_id}:{jti}` with a 7-day sliding TTL.
  - `POST /refresh`: Verifies cookie token against Redis, resets 7-day TTL, issues fresh access token.
  - `POST /logout`: Immediately invalidates the session key in Redis and clears cookies.
- **Redis Rate Limiting (`app/redis_client.py`)**:
  - IP-based rate limiting dependency (`Rate_Limit:{ip}:{path}`).
  - Email action rate limiting (`Rate_limit_Email:{email}:{action}`) preventing OTP & login brute force.
- **User Profile Endpoints**:
  - `GET /account/me`, `GET /account/{id}`, `PUT /account/{id}`, `DELETE /account/{id}`.

#### Remaining in Sprint 2 to Complete:

- [ ] **Role-Based Access Control (RBAC)**:
  - Add explicit FastAPI dependency guards: `require_role(["seller", "admin"])`.
- [ ] **Seller Onboarding Endpoint**:
  - Endpoint for verified users to create/update their `SellerProfile` (`POST /account/become-seller`).

#### Learning Goal:

> You must be able to explain the entire Auth lifecycle without notes:
> `Login Request -> Argon2 Verify -> Access Token (in memory) + Refresh Token (HttpOnly Cookie) -> Redis Session TTL -> Auto Renewal on /refresh -> Logout Blacklist/Deletion`.

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

1. **Wrap up Sprint 2**:
   - Implement role check dependencies (`require_seller`, `require_admin`).
   - Add the seller profile setup endpoint (`/account/become-seller`).
2. **Transition to Sprint 3**:
   - Create router `app/routers/services.py` for Event/Service CRUD.
   - Implement the `SELECT FOR UPDATE` locking mechanism for `POST /bookings`.
