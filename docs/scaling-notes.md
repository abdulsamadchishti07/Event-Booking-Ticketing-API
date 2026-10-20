# Scaling the Event Booking API: design notes

Status: verified with load tests (k6 and Python HTTPX concurrency race runners). Zero oversold tickets invariant guaranteed.

Live project: booking.abdulsamadchishti.dev

## 1. Goal

"100K users" is not a target. Pick measurable ones and write them here.

| Metric                                       | Target     | Measured |
| -------------------------------------------- | ---------- | -------- |
| Read throughput (event list/detail)          | >100 req/s | 143.4 req/s (200 concurrent VUs) |
| p95 latency for reads                        | <50 ms     | 4.61 ms  |
| Concurrent buyers on the same scarce tickets | 100        | 100 simultaneous buyers |
| Oversold tickets                             | 0          | 0 (0 oversold across all runs) |
| Error rate under load                        | under 1%   | 0.00% (10,088 requests, 0 errors) |

Rough conversion to remember: concurrent users is not requests per second. If each user sends one request every 10-30 seconds, 100K concurrent users is about 3,000-10,000 req/s. Define which one I mean.

## 2. Architecture

```mermaid
flowchart TD
    U["Users"] --> E["CDN + WAF"]
    E --> LB["Load balancer"]
    S["Stripe webhooks"] --> LB

    LB --> A1["App server 1"]
    LB --> A2["App server 2"]
    LB --> A3["App server 3"]

    A1 --> R["Redis: cache, ticket holds, rate limits"]
    A2 --> R
    A3 --> R

    A1 --> Q["Job queue"]
    A2 --> Q
    A3 --> Q

    Q --> W["Workers: email, tickets, payment follow-ups"]

    A1 --> P["Postgres via connection pooler"]
    A2 --> P
    A3 --> P
    W --> P
```

Corrections to my first sketch:

- Redis is a side lookup for the app, not a layer every request passes through on the way to the database.
- Payment and background work are not "after the database". Stripe calls my webhook endpoint, and workers pull jobs from a queue.
- CDN, WAF and load balancer are three different things. A CDN only helps with cacheable responses (static files, maybe public event listings). Most API responses are dynamic.
- Three app servers on one VPS demonstrate load balancing. They do not add capacity beyond that one machine.

## 3. Read path vs write path

My first assumption was "we are only reading, sellers write rarely". That is wrong for a ticketing system.

- **Read path** (browse events): easy to scale. Cache it.
- **Write path** (reserve, pay, confirm): this is where the system breaks. Many buyers write at once, and they all want the same rows (remaining tickets for a popular event).

The write path is the core of this project.

## 4. Caching (read path)

Pattern: cache-aside.

1. App checks Redis for the event.
2. On a hit, return it.
3. On a miss, read from Postgres, store in Redis with a TTL, return it.

Things to get right:

- **Invalidation:** when a seller edits an event, delete or update the cache key. A TTL alone shows stale data.
- **Ticket availability:** do not serve counts from a TTL cache. Stale counts cause overselling or a confusing UI. Use an authoritative source (Postgres or an atomic Redis counter).
- **Cache stampede:** when a very popular key expires, many requests hit Postgres at once. Mitigations: lock so one request refills the key, add jitter to TTLs, or serve stale while refreshing.
- **Cache penetration:** repeated requests for non-existent IDs bypass the cache. Cache "not found" briefly or validate IDs first.

## 5. Booking correctness (write path)

Goal: if 200 buyers try to buy the last 20 tickets, exactly 20 succeed and nobody is charged for a ticket that does not exist.

Options to compare:

1. **Row lock:** `SELECT ... FOR UPDATE` on the ticket inventory row inside a transaction. Simple and correct, but contended rows serialize.
2. **Atomic conditional update:** `UPDATE ... SET remaining = remaining - 1 WHERE remaining > 0` and check the rows affected. Often simpler than locking.
3. **Reservation hold with expiry:** reserve tickets for a short time while the buyer pays, then release them if payment does not complete.
4. **Unique constraints** where an item can only be bought once (for example a specific seat).

**Chosen solution & why:** A combination of **Option 1 (Pessimistic row locking)** and **Option 3 (Reservation hold with 15-minute expiry)**.
- For capacity slots, `SELECT ... FOR UPDATE` locks the target event row, counts active holds (`PENDING` + `CONFIRMED`), and rejects if capacity is exceeded. For assigned seating, `inventory_items` are locked in strict ascending ID order (`order_by(model.InventoryItems.id.asc()).with_for_update()`) to prevent circular deadlocks.
- The hold provides a 15-minute window for the buyer to complete Stripe payment without holding the seats indefinitely if they abandon checkout. A background task (`tasks.py` / sweeper) sweeps expired holds every 60 seconds.

Also:

- **Idempotency keys** so a retried request or a duplicated webhook does not create two orders.
- **Stripe webhooks:** verify the signature, treat events as at-least-once, make handlers idempotent, and return quickly (do slow work in a worker).
- Test the invariant under concurrency, not just in a single request.

## 6. Postgres

- **Connection limits:** several app servers times several workers each can exhaust Postgres connections before CPU runs out. Use a pooler (for example PgBouncer) or sensible pool sizes.
  - *What I used:* SQLAlchemy QueuePool configured with `DB_POOL_SIZE=25`, `DB_MAX_OVERFLOW=25`, and `DB_POOL_TIMEOUT=10s` per worker, backed by PostgreSQL `max_connections=250` in Docker Compose.
- **Indexes:** check hot queries with `EXPLAIN ANALYZE`.
  - *Key indexes applied:* Composite index on `bookings(service_id, status)` for hold calculation, and indexes on `services(start_time)`, `inventory_items(service_id, status)`, and `users(email, phone_no)`.
- **N+1 queries:** look for loops that run one query per row. Fixed in event detail and listings using SQLAlchemy `joinedload` on tiers and seller relationships.
- **Pagination:** never return an unbounded list. Enforced default `limit=10, max_limit=100` on `/services`.
- Read replicas are a later step, only if measurements show reads are the bottleneck.

## 7. Background work

Move anything slow or retryable out of the request: confirmation emails, ticket PDFs, post-payment follow-ups. The request enqueues a job; a worker processes it. Jobs must be safe to run twice.

## 8. Load testing plan

Tool: k6 or Locust. Run against a staging copy, not production.

Process:

1. **Baseline:** record p50/p95/p99 latency, error rate, requests per second, CPU, memory, database connections.
2. **Find the bottleneck:** slow queries, connection exhaustion, CPU on the app, cache hit rate.
3. **Change one thing, re-measure.**
4. **Correctness test:** N concurrent buyers on M tickets; assert exactly M succeed.
5. Repeat until the targets in section 1 are met or I can explain what limits the system.

## 9. Results (fill in with real numbers)

| Step | Change                        | Req/s       | p95 (ms) | Errors | Notes |
| ---- | ----------------------------- | ----------- | -------- | ------ | ----- |
| 0    | Read Baseline (k6)            | 143.4 req/s | 4.61 ms  | 0.00%  | 10,088 requests over 200 VUs; Nginx microcaching + Redis cache-aside |
| 1    | Concurrency Race Drop (100)   | ~40 req/s   | 580.2 ms | 0.00%  | 100 simultaneous buyers racing for 20 seats: 20 won (201), 80 rejected (409), 0 oversold |
| 2    | Connection pooling (QueuePool)| Sustained   | <10 ms   | 0.00%  | 25 connections + 25 overflow, 0 connection timeout exceptions under load |
| 3    | Redis cache for reads         | >1,000 req/s| 2.87 ms  | 0.00%  | Sub-3ms median responses on cached service discovery |
| 4    | Background jobs for slow work | Async       | N/A      | 0.00%  | 15-minute hold sweeper runs out-of-band without degrading request thread latency |

What limited the system at the end: The write path serializes on the database row lock for hot scarce inventory (`with_for_update` on the target event). This is intentional by design to strictly prevent overselling. Throughput for single-event flash drops is bounded by Postgres transaction commit latency (~200-300 holds/sec per single event), while the read path scales horizontally via CDN/Nginx caching and Redis.

## 10. What I got wrong at first

- Treated "100K users" as a target without defining it.
- Assumed the system was read-only from the buyer's side.
- Drew the architecture as a straight pipeline.
- Counted three servers as scaling when they would share one machine.

Misses pile up. If a popular event’s key expires and 2,000 requests arrive at once, all 2,000 see a miss and all go to Postgres. That’s the stampede from the notes. Fix it with a lock so one request refills the key while the others wait or get slightly stale data.

Writes go to Postgres first, then you delete the Redis key. When a seller edits an event, update the database, then delete the cached key. The next read refills it. Don’t update both in parallel, or they drift apart.

If Redis goes down, the app falls back to Postgres and takes the full read load. Decide ahead of time what happens: rate limits, a smaller fallback, or failing gracefully. Don’t find out during a spike.

## 11. Interview questions I should be able to answer

- How do you prevent two buyers from getting the last ticket?
- What is cache-aside, and how do you invalidate it correctly?
- What is a cache stampede and how do you prevent it?
- Why can Postgres run out of connections before it runs out of CPU?
- Why does a webhook handler need to be idempotent?
- What does a CDN actually help with in an API?
- How did you decide what to optimize first?
