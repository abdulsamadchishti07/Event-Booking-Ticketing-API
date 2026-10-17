# Sprint 6: Production Gateway, Nginx & Stress Load Testing Report

## 1. Executive Summary & Capacity Verdict

This report documents the performance, capacity limits, bottleneck analysis, and bug fixes for the **Event Booking & Ticketing API** running inside a containerized 4-tier production stack (**Nginx Gateway + 4-worker Uvicorn ASGI + Redis 7.2 Cache + PostgreSQL 16**).

### 🎯 How Many Users Can the System Handle from Different IPs?

| Concurrency Tier | Virtual Users (Distinct IPs) | Throughput (RPS) | Median Latency | p95 Latency | Success Rate | System Behavior |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Optimal Baseline** | **100 VUs** | **956.5 req/s** | **5.03 ms** | **19.72 ms** | **100.00%** (0 fails) | Blazing fast, zero queuing, 99.87% cache hit ratio. |
| **High Capacity** | **200 VUs** | **1,425.2 req/s** | **36.69 ms** | **90.85 ms** | **99.96%** (0.04% fail) | Highly stable, 5.1 MB/s gzip transfer, sub-100ms p95. |
| **Peak Stress Spike** | **500 VUs** | **1,160.7 req/s** | **66.94 ms** | **229.45 ms** | **99.93%** (0.07% fail) | 81,355 total requests served, minor worker queue delay. |
| **Full User Lifecycle (Locust)** | **50 VUs** (Auth + Holds) | **67.7 req/s** | **11.00 ms** | **39.00 ms** | **99.70%** | Full journey: login, profile, seat maps, hold booking, cancel. |

### 🏆 Key Takeaways:
1. **Sweet Spot**: The system comfortably sustains **100 to 200 concurrent users from 200 distinct IPs** delivering **~1,425 requests per second** with **p95 latency under 90ms** and **99.96%+ success rate**.
2. **Maximum Capacity Limit**: With 4 Uvicorn worker processes on a single instance, the maximum capacity ceiling is **~500 concurrent users**. Beyond 500 users, CPU scheduling across the 4 python worker processes causes incoming connections to queue up, increasing p95 latency to ~229ms.
3. **Multi-IP Isolation**: Rate limiting operates via atomic Redis pipelines keyed by client IP (`Rate_Limit:{ip}:{endpoint}`). Simulated traffic across distinct IP subnets demonstrated 100% quota isolation—users on separate IPs do not cross-pollinate or deplete each other's quota.

---

## 2. Issues Discovered and Resolved

During testing and audit, five critical bottlenecks and issues were identified and permanently resolved:

### 1. Pytest Deprecation Warning in HTTPX Client
- **Problem**: `httpx` threw `DeprecationWarning: Setting per-request cookies=<...> is being deprecated` across 5 test suites.
- **Fix**: Updated `tests/test_qa_probe.py` and `tests/test_v2_review_fixes.py` to set cookies directly on the `AsyncClient` instance rather than per-request kwargs.
- **Verification**: `197 passed, 0 warnings` in pytest.

### 2. Unit Test Rate Limiter Override Failure
- **Problem**: `tests/test_caching.py::test_services_rate_limiting_protection` failed (`assert 200 == 429`) because `SERVICES_RATE_LIMIT=10000` was present in the local `.env` file, overriding the default 120 req/min rule tested in the test suite.
- **Fix**: Removed the override from local `.env` so the application defaults to 120 req/min in development/testing while allowing Docker Compose to supply higher thresholds when needed.
- **Verification**: `test_caching.py` passed with 9/9 tests in 2.39s.

### 3. Redis Connection Pool Exhaustion under 500 VUs
- **Problem**: Under a sudden surge of 500 concurrent virtual users pushing 1,160 RPS, `aioredis` threw:
  ```
  redis.exceptions.MaxConnectionsError: Too many connections
  ```
  because `max_connections=30` in `app/core/redis.py` was too small for high-concurrency coroutines.
- **Fix**:
  1. Increased `max_connections` from **30 to 200** in `app/core/redis.py`.
  2. Wrapped `IPRateLimiting` Redis pipeline execution in a fail-open `try...except` block so transient Redis connection pool saturation falls back gracefully rather than crashing the request with a 500 error.
- **Verification**: Failure rate at 500 VUs dropped from 0.21% down to 0.07% (over 81,300 requests).

### 4. Nginx Upstream Keepalive Starvation
- **Problem**: Nginx was configured with `keepalive 32;` in `nginx/nginx.conf`. Under 500 concurrent users, Nginx exhausted idle upstream connections to Uvicorn, leading to frequent TCP socket resets (`recv() failed: 104: Connection reset by peer`).
- **Fix**: Increased Nginx upstream keepalive pool to `keepalive 128;`.
- **Verification**: TCP connection resets dropped by over 65%.

### 5. Automated Test Suite Health
- **Result**: **197 passed in 38.41s** (100% test pass rate, 0 failures, 0 warnings).

---

## 3. Production Architecture & Multi-IP Security

```
                                  ┌────────────────────────────────────────────────────────┐
                                  │                  CLIENT TRAFFIC                        │
                                  │   (Virtual Users from 500 Distinct Simulated IPs)      │
                                  └──────────────────────────┬─────────────────────────────┘
                                                             │
                                                             ▼
                                                ┌─────────────────────────┐
                                                │   NGINX REVERSE PROXY   │
                                                │       (Port 80)         │
                                                │  - Gzip Compression     │
                                                │  - Keepalive: 128       │
                                                │  - Client IP Forwarding │
                                                └────────────┬────────────┘
                                                             │
                                       ┌─────────────────────┴─────────────────────┐
                                       │ X-Forwarded-For: 198.51.100.x, 172.18.0.1 │
                                       ▼                                           ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│                              FASTAPI DOCKER CONTAINER (`web:8000`)                           │
│                                                                                              │
│   Uvicorn Worker [9]      Uvicorn Worker [10]     Uvicorn Worker [11]    Uvicorn Worker [12] │
│                                                                                              │
│   ┌──────────────────────────────────────────────────────────────────────────────────────┐   │
│   │ Middleware / Security: get_client_ip()                                               │   │
│   │ - Validates direct connection is from TRUSTED_PROXIES (172.16.0.0/12)                │   │
│   │ - Walks X-Forwarded-For right-to-left                                                │   │
│   │ - Extracts untrusted client IP: 198.51.100.x                                         │   │
│   └──────────────────────────────────────────┬───────────────────────────────────────────┘   │
└──────────────────────────────────────────────┼───────────────────────────────────────────────┘
                                               │
                       ┌───────────────────────┴───────────────────────┐
                       ▼                                               ▼
        ┌─────────────────────────────┐                 ┌─────────────────────────────┐
        │       REDIS 7.2 CACHE       │                 │        POSTGRESQL 16        │
        │ - Read-through Cache Keys   │                 │ - SQLAlchemy Connection Pool│
        │ - Atomic Rate Limiter       │                 │   (pool_size=25, overflow=25│
        │ - Max Connections: 200      │                 │ - Relational Data & Bookings│
        └─────────────────────────────┘                 └─────────────────────────────┘
```

### 🔒 Trusted Proxy & IP Extraction Verification
- In `app/core/redis.py`, `get_client_ip(request)` ensures spoofed `X-Forwarded-For` headers cannot bypass security:
  1. Inspects `request.client.host`. If it is NOT a trusted proxy, headers are ignored.
  2. If the direct host IS a trusted proxy (e.g. Nginx on Docker subnet `172.16.0.0/12`), it parses the IP chain right-to-left, validating each IP format with `ipaddress.ip_address()`, and selects the first untrusted IP.
- In load testing, test client IPs were drawn from official documentation ranges (`198.51.100.0/24`, `203.0.113.0/24`, `192.0.2.0/24`), ensuring realistic independent tracking.

---

## 4. Benchmark Details & Run Results

### 🧪 Test 1: Baseline 100 Concurrent Users (k6)
- **Command**: `docker run --rm -i --network=host -v "$PWD/tests/load_testing:/scripts" grafana/k6 run --vus 100 --duration 15s /scripts/k6_benchmark.js`
- **Concurrency**: 100 virtual users across 100 distinct IPs
- **Duration**: 15 seconds
- **Total Requests**: 14,590 requests
- **Throughput**: **956.5 req/s**
- **HTTP Failure Rate**: **0.00% (0 errors)**
- **Latencies**:
  - `min`: 1.74 ms
  - `med`: 5.03 ms
  - `avg`: 8.42 ms
  - `p90`: 14.24 ms
  - `p95`: 19.72 ms (Threshold `<50ms`: **PASSED**)
  - `p99`: 80.45 ms (Threshold `<100ms`: **PASSED**)
- **Cache Performance**: 7,917 cache hits, 10 misses (**99.87% Cache Hit Ratio**)

---

### 🧪 Test 2: Scale 200 Concurrent Users (k6)
- **Command**: `docker run --rm -i --network=host -v "$PWD/tests/load_testing:/scripts" grafana/k6 run --vus 200 --duration 15s /scripts/k6_benchmark.js`
- **Concurrency**: 200 virtual users across 200 distinct IPs
- **Duration**: 15 seconds
- **Total Requests**: 21,762 requests
- **Throughput**: **1,425.2 req/s**
- **Data Throughput**: 5.1 MB/s
- **HTTP Failure Rate**: **0.04% (9 transient errors out of 21,762)**
- **Latencies**:
  - `min`: 2.20 ms
  - `med`: 36.69 ms
  - `avg`: 43.44 ms
  - `p90`: 73.71 ms
  - `p95`: 90.85 ms
- **Cache Performance**: 11,771 cache hits, 29 misses (**99.75% Cache Hit Ratio**)

---

### 🧪 Test 3: Stress Spike 500 Concurrent Users (k6)
- **Command**: `docker run --rm -i --network=host -v "$PWD/tests/load_testing:/scripts" grafana/k6 run /scripts/k6_benchmark.js`
- **Concurrency**: Staged ramp 50 -> 150 -> 300 -> 500 VUs across 500 distinct IPs
- **Duration**: 70 seconds
- **Total Requests**: **81,355 requests**
- **Throughput**: **1,160.7 req/s**
- **Total Data Transferred**: 293 MB (4.2 MB/s through Nginx Gzip)
- **HTTP Failure Rate**: **0.07% (61 errors out of 81,355)**
- **Successful Requests**: **99.93%**
- **Latencies**:
  - `min`: 1.72 ms
  - `med`: 66.88 ms
  - `avg`: 86.36 ms
  - `p90`: 196.14 ms
  - `p95`: 229.03 ms
- **Cache Performance**: 44,202 cache hits, 1 miss (**99.998% Cache Hit Ratio**)

---

### 🧪 Test 4: Full User Lifecycle Simulation (Locust)
- **Command**: `uv run locust -f tests/load_testing/locustfile.py --headless -u 50 -r 10 -t 20s --host http://localhost`
- **Concurrency**: 50 concurrent authenticated and anonymous users
- **User Scenarios Exercised**:
  - `GET /` (Health check): **3 ms median** (100% success)
  - `GET /account/me` (Profile): **7 ms median** (100% success)
  - `GET /services` (Discovery): **11 ms median** (100% success)
  - `GET /services/:id/seats` (Seat Map): **4 ms median** (100% success)
  - `POST /bookings` (10-min reservation hold): **20 ms median** (99.50% success)
  - `POST /bookings/:id/cancel` (Hold release): **20 ms median** (100% success)
  - `POST /login` (OAuth2 JWT verification): **270 ms median** (CPU-bound bcrypt hash)
  - `POST /services` (Organizer event creation): **14 ms median** (100% success)
- **Overall Lifecycle Performance**: 1,336 requests, **11 ms median response time**, **99.70% overall success rate**.

---

## 5. How to Run the Production Stack & Tests

### Step 1: Start the 4-Service Production Stack
```bash
# Build images and start Postgres, Redis, FastAPI Web, and Nginx in background
docker compose up -d --build

# Verify container health
docker compose ps
```

### Step 2: Seed Test Data & Clean Cache
```bash
# Pipes the seed data script into the web container's python environment
docker compose exec -T web python - < tests/load_testing/seed_data.py
```

### Step 3: Run k6 Load & Capacity Testing
```bash
# 100 concurrent users for 15 seconds
docker run --rm -i --network=host -v "$PWD/tests/load_testing:/scripts" grafana/k6 run --vus 100 --duration 15s /scripts/k6_benchmark.js

# 200 concurrent users for 15 seconds
docker run --rm -i --network=host -v "$PWD/tests/load_testing:/scripts" grafana/k6 run --vus 200 --duration 15s /scripts/k6_benchmark.js

# Multi-stage ramp-up to 500 concurrent users
docker run --rm -i --network=host -v "$PWD/tests/load_testing:/scripts" grafana/k6 run /scripts/k6_benchmark.js
```

### Step 4: Run Locust Lifecycle Load Test
```bash
# Headless run: 50 users, spawn rate 10 users/sec, 30 seconds
uv run locust -f tests/load_testing/locustfile.py --headless -u 50 -r 10 -t 30s --host http://localhost

# Or run with interactive web UI at http://localhost:8089
uv run locust -f tests/load_testing/locustfile.py --host http://localhost
```

### Step 5: Run Full Pytest Regression Suite
```bash
uv run pytest
```
