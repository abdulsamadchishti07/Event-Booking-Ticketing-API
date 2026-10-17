# Event Booking & Ticketing API

A high-concurrency event booking and ticketing backend built with **FastAPI**, **PostgreSQL 16**, **Redis 7**, and **Stripe**.

---

## 🛠 Tech Stack

- **Framework**: FastAPI (Python 3.12+ managed via `uv`)
- **Database**: PostgreSQL 16+ with SQLAlchemy 2.0 (Mapped ORM) & Alembic migrations
- **In-Memory Cache & Session Store**: Redis 7.0+ (`redis.asyncio`) with distributed leader election locks
- **Authentication**: OAuth2 with Argon2id (`pwdlib`), signed JWTs (`PyJWT`), HttpOnly Refresh Cookies
- **Payments**: Stripe PaymentIntents, automated webhooks with HMAC-SHA256 signature verification, and idempotent refunds
- **Concurrency Control**: PostgreSQL row-level pessimistic locking (`SELECT FOR UPDATE`), Redis distributed worker locks, and atomic hold sweeper tasks

---

## ⚡ Prerequisites

- **Python**: 3.12 or newer (recommended to use [uv](https://github.com/astral-sh/uv))
- **PostgreSQL**: 16.0 or newer
- **Redis**: **7.0 or newer (Strict Requirement)**
  > ⚠️ **Important**: The application uses the `EXPIRE ... NX` command modifier (`expire(..., nx=True)`) for distributed rate limiting and token locking, which was introduced in **Redis 7.0**. Older versions of Redis (such as Redis 6) will fail.

---

## 🚀 Getting Started

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/abdulsamadchishti07/Event-Booking-Ticketing-API.git
cd Event-Booking-Ticketing-API

# Install dependencies using uv
uv sync
```

### 2. Environment Configuration

Copy `.env.example` to `.env` and fill in your local settings:

```bash
cp .env.example .env
```

Key configuration parameters:
- `DATABASE_URL`: PostgreSQL connection URI (`postgresql://user:pass@localhost:5432/event_booking`)
- `REDIS_URL`: Redis 7+ URI (`redis://localhost:6379/0`)
- `SECRET_KEY`: Random 64-character secret key for JWT signing
- `STRIPE_SECRET_KEY` & `STRIPE_WEBHOOK_SECRET`: Stripe API keys
- `TRUSTED_PROXIES`: Comma-separated list of trusted reverse proxy IPs/CIDRs (e.g. `127.0.0.1, 10.0.0.0/8`). When deployed behind Nginx, this must include the proxy's IP address.

### 3. Database Migrations

Apply database migrations using Alembic:

```bash
uv run alembic upgrade head
```

Verify migration status:

```bash
uv run alembic check
```

### 4. Run Development Server

```bash
uv run uvicorn app.main:app --reload --port 8000
```

---

## 🔒 Security & Architecture Highlights

- **Anti-Lockout & Brute-Force Defense**:
  - Failed logins and lockouts are tracked per `{email}:{client_ip}` to prevent external denial-of-service against legitimate users.
  - Lockout status is checked *before* password verification (`403 Forbidden` for locked clients).
  - A global per-email threshold (25 failed attempts) mitigates distributed botnet attacks.
- **Refresh Token Rotation & Grace Window**:
  - Tokens rotate on every refresh. Replay attacks revoke the entire user session family.
  - A 30-second grace window prevents race conditions when multiple browser tabs refresh simultaneously.
- **Anti-Seat-Hoarding & Overbooking Prevention**:
  - 10-minute hold duration for pending reservations managed by an asynchronous background hold sweeper.
  - Max 10 tickets per booking and max 5 active pending holds per customer.
  - Row-level locking (`with_for_update()`) ordered deterministically (`ORDER BY id ASC`) prevents deadlocks and overbooking under high concurrency.
- **Stripe Webhook Resiliency**:
  - Webhooks verify cryptographic signatures (`stripe-signature`).
  - Webhook failures trigger automatic refunds with deterministic idempotency keys (`auto_refund_{pi_id}`).
  - Failed payments (`payment_intent.payment_failed`) record failure without prematurely cancelling bookings, allowing customer retries until the hold expires.

---

## ⚡ Concurrency Performance & Scaling Architecture

The API is architected for high-traffic ticket drops with distinct performance characteristics between single-node standalone deployments and distributed scale:

```
================================================================================
  STANDALONE MEASURED CAPACITY (1 Node, 4 Uvicorn Workers, Postgres, Redis):
  - Peak Origin Read Throughput: 1,545.3 Requests / Second (Redis-cached)
  - Booking Hold Throughput: ~160 Requests / Second (Postgres SELECT FOR UPDATE)
  - Auth Login Throughput: 25 – 30 Requests / Second (Argon2id CPU cost)
  - Standalone Concurrent Active Users: 5,000 – 10,000 CCU (at 6.5s think time)

  DISTRIBUTED TARGET CAPACITY (Multi-Node + Edge CDN + Read Replicas):
  - Theoretical System Capacity: 15,453 Requests / Second (with 90% CDN offload)
  - Target Scale: 100,000+ Concurrent Active Users (CCU)
================================================================================
```

### 🧮 Capacity Modeling: Little's Law ($N = \lambda \times W$)

$$N = \lambda \times W$$

1. **Standalone Single-Node Capacity**:
   - At measured peak origin throughput of $\lambda = 1,545\text{ RPS}$ and average human think time of $W = 6.5\text{s}$, a single instance comfortably sustains:
     $$N = 1,545 \times 6.5 \approx \mathbf{10,000 \text{ Concurrent Active Users (CCU)}}$$
2. **Distributed Production Scale (100K CCU)**:
   - To sustain 100,000 CCU, public discovery endpoints (`GET /services`, `GET /locations`) are fronted by an Edge CDN (e.g. Cloudflare) absorbing ~90% of traffic. Dynamic booking mutations and authenticated flows hit origin workers horizontally scaled behind a load balancer with PostgreSQL read replicas (detailed in [SCALING_100K.md](SCALING_100K.md)).

---

### 📊 Measured Per-API Capacity Matrix

Empirical benchmarks run via `tests/load_testing/k6_per_api_benchmarks.js` across 74,796 requests to identify subsystem constraints:

| Subsystem & Endpoint | Measured Peak Origin RPS | Median Latency | p95 Latency | Success Rate | Primary Bottleneck / Protection | Scale Architecture |
| :--- | :---: | :---: | :---: | :---: | :--- | :---: |
| **1. Health Check**<br>`GET /` | **2,524 RPS** | **6.61 ms** | **33.39 ms** | **100.00%** | **ASGI Loop Overhead**: Pure Uvicorn context switching. | 🟢 Standalone 160K+ CCU |
| **2. Seat Map Inspection**<br>`GET /services/1/seats` | **1,487 RPS** | **49.66 ms** | **113.06 ms** | **100.00%** | **Serialization**: JSON encoding cached in Redis. | 🟢 Standalone 10K / CDN 100K |
| **3. Event Discovery**<br>`GET /services?limit=20` | **1,275 RPS** | **179.31 ms** | **271.11 ms** | **100.00%** | **Compression**: Nginx gzip reduces payload to 13 MB/s. | 🟢 Standalone 10K / CDN 100K |
| **4. User Profile Read**<br>`GET /account/me` | **762 RPS** | **108.06 ms** | **307.02 ms** | **93.70%** | **DB Connections**: Saturated at 250 concurrent VUs. | 🟡 Add Redis user profile cache |
| **5. Seat Reservation Hold**<br>`POST /bookings` | **160 RPS** | **165.76 ms** | **375.88 ms** | **99.90%** | **PostgreSQL Locks**: `SELECT FOR UPDATE` serializes uncommitted seat rows. | 🟡 Add Redis pre-hold queue |
| **6. Password Hashing**<br>`POST /login` | **25 – 30 RPS** | **1,110 ms** | **1,850 ms** | **100.00%** | **Argon2id Cost**: Intentionally tuned to resist GPU brute-force attacks. | 🟢 Working as designed |

---

### 💳 Payment Subsystem & Stripe Integration

- **Synchronous Intent Throughput**: Bound by Stripe API default rate limits (~100 req/s live, ~25 req/s test) and network latency (~180ms round-trip).
- **Asynchronous Webhook Settlement**: **450 – 600 webhooks / second** processed internally, transitioning booking state from `pending` to `confirmed`.
- **Zero Double-Billing via Idempotency**: All payment intents and refund calls use deterministic idempotency keys (`booking_{id}_{attempt}`).
- **Resiliency**: HMAC-SHA256 signature verification (`stripe-signature`). Failed card charges preserve the 10-minute hold window so customers can retry payment without losing their reserved seats.

> 📖 **Full Engineering Analysis & Reproduction Details**: See [BENCHMARK_100K_PROOF.md](BENCHMARK_100K_PROOF.md) and [SCALING_100K.md](SCALING_100K.md).

---

## 🧪 Testing

Run the comprehensive automated test suite (197 tests passing with 0 warnings):

```bash
uv run pytest
```

