Project: Event Booking / Ticketing API

Why this one and not something generic: it forces you to hit real, hard problems instead of tutorial-level CRUD — which is the whole point since you said "a bit complex."

Core scope:

Users can browse events, reserve seats/tickets, and pay.
Admins can create events with limited seat capacity.

How it maps to what you asked for:

DB design (Postgres): Users, Events, Seats/Tickets, Bookings, Payments tables with real foreign
key relationships. The hard part: preventing overselling — two users hitting "book seat #5" at
the same second. This forces you to learn row-level locking / SELECT FOR UPDATE / transactions
properly, not just theory.

3rd party API: Stripe (or a sandbox payment gateway) for actual payment processing. This is your
integration requirement — real API key handling, webhooks for payment confirmation (Stripe sends
you an async callback when payment succeeds/fails — good real-world lesson on webhook security/
signature verification).

JWT / Auth: Access + refresh token flow, role-based access (admin vs regular user) since admins
create events and users book them.

Redis caching: Cache event listings (read-heavy, changes rarely) — cache invalidation when an
admin updates an event is a real skill, not decorative. Also use Redis for rate limiting (see
below) and optionally as a lock for seat reservation (SETNX pattern) to prevent race conditions
before the DB transaction even hits.

Securing the API: Input validation via Pydantic, rate limiting per user/IP (Redis-backed), proper
password hashing, protecting admin-only routes, securing the Stripe webhook endpoint (signature
verification, not just "trust whatever hits this URL").

Nginx: Deploy FastAPI behind Nginx as reverse proxy. Bonus: run 2+ Uvicorn workers and let Nginx
load-balance between them — now you've actually built the thing you were asking about, not just
reading about it.

Response time / load handling: Use locust or k6 to load test your API. Measure response time with
and without Redis caching on the event-listing endpoint — this gives you a real before/after
number to talk about in an interview ("caching cut average response time from X ms to Y ms
under N concurrent users"), which is infinitely more credible than saying "I used Redis."
