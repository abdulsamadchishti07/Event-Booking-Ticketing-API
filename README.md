# Event Booking & Ticketing API

A high-concurrency event booking and ticketing backend built with **FastAPI**, **PostgreSQL 16**, **Redis 7**, and **Stripe**.

---

## 🛠 Tech Stack

- **Framework**: FastAPI (Python 3.12+ managed via `uv`)
- **Database**: PostgreSQL 16+ with SQLAlchemy 2.0 (Mapped ORM) & Alembic migrations
- **In-Memory Cache & Session Store**: Redis 7.0+ (`redis.asyncio`)
- **Authentication**: OAuth2 with Argon2id (`pwdlib`), signed JWTs (`PyJWT`), HttpOnly Refresh Cookies
- **Payments**: Stripe PaymentIntents, automated webhooks with HMAC-SHA256 signature verification, and idempotent refunds
- **Concurrency Control**: Pessimistic locking (`SELECT FOR UPDATE`), Redis distributed locks, and hold sweeper tasks

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
cd "Event Booking - Ticketing API"

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

## 🧪 Testing

Run the comprehensive automated test suite (171 tests passing):

```bash
uv run pytest
```
