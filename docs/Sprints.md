1 — Foundations & DB design
Design schema (Users, Events, Seats, Bookings, Payments) in Postgres. Draw the ER diagram first,
on paper or draw.io, before writing a single model. Get relationships and constraints right
(unique constraints on seat+event, foreign keys).

2 — Auth
JWT access+refresh tokens, Argon2id password hashing, role-based access (admin/user). Finish this
properly — no moving on until you can explain the full login → token → protected route flow
without notes.

3 — Core booking logic + concurrency
Event CRUD (admin), seat booking (user), and the hard part: preventing double-booking under
concurrent requests. Learn and implement SELECT FOR UPDATE or Redis-based locking here. This is
the week you'll feel dumbest. That's correct.

4 — Stripe integration
Payment flow, webhook endpoint, signature verification. Read Stripe's docs directly, don't let AI
summarize them for you — payment webhook security is not a place to have shaky understanding.

5 — Redis caching + rate limiting
Cache event listings, invalidate on update, add per-user/IP rate limiting.

6 — Deployment + load testing
Dockerize, put behind Nginx with multiple Uvicorn workers, then hit it with Locust/k6 and record
real before/after numbers (with vs without cache).
