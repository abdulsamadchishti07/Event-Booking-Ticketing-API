"""
Locust Full-System Load Testing Suite.
Simulates real-world high-concurrency traffic across the entire Event Booking & Ticketing API:
- Gateway & Health Checks
- Location & Venue Browsing
- Public Event Discovery & Filter/Search
- Live Seat Maps & Reviews
- User Authentication & Profile
- 10-Minute Reservation Holds (Slot-Capacity & Unit-Assigned)
- Active Booking Management & Cancellations
- Organizer Event Creation & Pricing Tiers
"""

import random
from datetime import datetime, timedelta, timezone
from locust import HttpUser, task, between, tag


def get_random_client_ip(user_instance) -> str:
    """
    Generates a unique simulated client IP per virtual user instance.
    Parsed by the API's trusted proxy middleware to test independent rate limit quotas.
    """
    return f"198.51.100.{(abs(id(user_instance)) % 250) + 1}"


# ============================================================================
# 1. Anonymous Public Explorer (Traffic Weight: 40)
# ============================================================================
class AnonymousExplorerUser(HttpUser):
    """
    Simulates unregistered visitors browsing venues, searching events,
    inspecting seat maps, and checking past reviews.
    Exercises Redis caching (sub-5ms cache hits vs DB misses).
    """
    weight = 40
    wait_time = between(0.2, 0.8)

    def on_start(self):
        self.client_ip = get_random_client_ip(self)
        self.client.headers.update({"X-Forwarded-For": self.client_ip})

    @tag("discovery", "locations")
    @task(3)
    def browse_locations(self):
        params = random.choice([
            {"city": "Lahore"},
            {"city": "Islamabad"},
            {"city": "Karachi"},
            {}
        ])
        with self.client.get("/locations", params=params, name="GET /locations (Venues)", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Failed /locations with status {resp.status_code}")

    @tag("discovery", "events")
    @task(6)
    def browse_services_list(self):
        params = random.choice([
            {"city": "Lahore"},
            {"city": "Islamabad"},
            {"sort_by": "date_asc"},
            {"sort_by": "price_asc"},
            {"limit": 20},
            {"limit": 10},
            {}
        ])
        with self.client.get("/services", params=params, name="GET /services (Search & Filter)", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Failed /services with status {resp.status_code}")

    @tag("discovery", "events")
    @task(4)
    def view_service_detail(self):
        service_id = random.choice([1, 2, 3])
        with self.client.get(f"/services/{service_id}", name="GET /services/:id (Event Detail)", catch_response=True) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            else:
                resp.failure(f"Failed /services/{service_id} with status {resp.status_code}")

    @tag("discovery", "seats")
    @task(3)
    def view_seat_map(self):
        # Service 1 is unit_assigned with live seat map
        with self.client.get("/services/1/seats", name="GET /services/:id/seats (Seat Map)", catch_response=True) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            else:
                resp.failure(f"Failed /services/1/seats with status {resp.status_code}")

    @tag("discovery", "reviews")
    @task(2)
    def view_service_reviews(self):
        service_id = random.choice([1, 3])
        with self.client.get(f"/services/{service_id}/reviews", name="GET /services/:id/reviews (Reviews)", catch_response=True) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            else:
                resp.failure(f"Failed reviews with status {resp.status_code}")


# ============================================================================
# 2. Registered Ticket Buyer (Traffic Weight: 50)
# ============================================================================
class CustomerBuyerUser(HttpUser):
    """
    Simulates authenticated customers:
    - Logs in to obtain JWT access token
    - Inspects profile and upcoming events
    - Places 10-minute temporary reservation holds (POST /bookings)
    - Checks booking history (GET /bookings/me)
    - Cancels/releases held bookings (POST /bookings/:id/cancel)
    """
    weight = 50
    wait_time = between(0.3, 1.2)

    def on_start(self):
        self.client_ip = get_random_client_ip(self)
        self.client.headers.update({"X-Forwarded-For": self.client_ip})
        self.access_token = None
        self.my_hold_booking_ids = []

        # Rotate among the 20 seeded loadtest buyers
        buyer_idx = random.randint(1, 20)
        self.email = f"buyer{buyer_idx}@loadtest.com"
        self.password = "Password123!"

        # Authenticate via OAuth2 form-data
        resp = self.client.post(
            "/login",
            data={"username": self.email, "password": self.password},
            name="POST /login (Buyer Auth)"
        )
        if resp.status_code == 200:
            data = resp.json()
            self.access_token = data.get("access_token")
            self.client.headers.update({"Authorization": f"Bearer {self.access_token}"})

    @tag("buyer", "profile")
    @task(2)
    def view_profile(self):
        if not self.access_token:
            return
        with self.client.get("/account/me", name="GET /account/me (Buyer Profile)", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Failed /account/me: {resp.status_code}")

    @tag("buyer", "bookings")
    @task(5)
    def create_booking_hold(self):
        """
        Creates a 10-minute temporary reservation hold on slot-capacity event (service_id=2).
        If user approaches the 5-hold maximum cap, releases an older hold first.
        """
        if not self.access_token:
            return

        # If holding 4 or more, release one to prevent hitting MAX_PENDING_HOLDS limit (5)
        if len(self.my_hold_booking_ids) >= 4:
            self.cancel_oldest_hold()

        payload = {
            "service_id": 2,
            "quantity": 1,
            "tier_id": 3
        }
        with self.client.post("/bookings", json=payload, name="POST /bookings (Hold Reservation)", catch_response=True) as resp:
            if resp.status_code == 201:
                booking_data = resp.json()
                b_id = booking_data.get("id")
                if b_id:
                    self.my_hold_booking_ids.append(b_id)
                resp.success()
            elif resp.status_code == 400 and "maximum limit" in resp.text:
                # Expected when active holds exceed cap; treat as controlled test behavior
                self.cancel_oldest_hold()
                resp.success()
            else:
                resp.failure(f"Failed /bookings: {resp.status_code} - {resp.text[:80]}")

    @tag("buyer", "bookings")
    @task(4)
    def view_my_bookings(self):
        if not self.access_token:
            return
        with self.client.get("/bookings/me", name="GET /bookings/me (My Bookings)", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Failed /bookings/me: {resp.status_code}")

    @tag("buyer", "bookings")
    @task(2)
    def view_booking_detail(self):
        if not self.access_token or not self.my_hold_booking_ids:
            return
        booking_id = random.choice(self.my_hold_booking_ids)
        with self.client.get(f"/bookings/{booking_id}", name="GET /bookings/:id (Booking Detail)", catch_response=True) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            else:
                resp.failure(f"Failed /bookings/{booking_id}: {resp.status_code}")

    @tag("buyer", "bookings")
    @task(3)
    def cancel_held_booking(self):
        """
        Simulates customer deciding not to complete checkout, releasing the hold back to inventory.
        """
        self.cancel_oldest_hold()

    def cancel_oldest_hold(self):
        if not self.access_token or not self.my_hold_booking_ids:
            return
        booking_id = self.my_hold_booking_ids.pop(0)
        payload = {"reason": "Customer cancelled during load test"}
        with self.client.post(
            f"/bookings/{booking_id}/cancel",
            json=payload,
            name="POST /bookings/:id/cancel (Cancel Hold)",
            catch_response=True
        ) as resp:
            if resp.status_code in (200, 400, 404):
                resp.success()
            else:
                resp.failure(f"Failed cancel {booking_id}: {resp.status_code}")


# ============================================================================
# 3. Event Organizer / Seller (Traffic Weight: 10)
# ============================================================================
class EventOrganizerUser(HttpUser):
    """
    Simulates event organizers managing their portfolio:
    - Logs in as verified seller
    - Inspects venue locations (GET /locations)
    - Adds new venue locations (POST /locations)
    - Creates new upcoming events (POST /services)
    - Configures pricing tiers (POST /services/:id/tiers)
    """
    weight = 10
    wait_time = between(1.0, 3.0)

    def on_start(self):
        self.client_ip = get_random_client_ip(self)
        self.client.headers.update({"X-Forwarded-For": self.client_ip})
        self.access_token = None
        self.created_service_ids = []

        resp = self.client.post(
            "/login",
            data={"username": "seller@loadtest.com", "password": "Password123!"},
            name="POST /login (Seller Auth)"
        )
        if resp.status_code == 200:
            data = resp.json()
            self.access_token = data.get("access_token")
            self.client.headers.update({"Authorization": f"Bearer {self.access_token}"})

    @tag("seller", "venues")
    @task(3)
    def view_venues(self):
        if not self.access_token:
            return
        with self.client.get("/locations", name="GET /locations (Seller Venues)", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Failed /locations: {resp.status_code}")

    @tag("seller", "events")
    @task(2)
    def publish_new_event(self):
        if not self.access_token:
            return

        rand_suffix = random.randint(1000, 999999)
        now = datetime.now(timezone.utc)
        start_time = (now + timedelta(days=random.randint(15, 60))).isoformat()
        end_time = (now + timedelta(days=random.randint(61, 90))).isoformat()

        payload = {
            "service_name": f"LoadTest Event {rand_suffix}",
            "service_desc": "High performance event generated during load test",
            "location_id": 1,
            "booking_mode": "slot_capacity",
            "max_capacity": 5000,
            "base_price": 50.00,
            "start_time": start_time,
            "end_time": end_time
        }
        with self.client.post("/services", json=payload, name="POST /services (Create Event)", catch_response=True) as resp:
            if resp.status_code == 201:
                svc_data = resp.json()
                svc_id = svc_data.get("id")
                if svc_id:
                    self.created_service_ids.append(svc_id)
                resp.success()
            else:
                resp.failure(f"Failed /services: {resp.status_code}")

    @tag("seller", "tiers")
    @task(1)
    def add_pricing_tier(self):
        if not self.access_token or not self.created_service_ids:
            return

        svc_id = random.choice(self.created_service_ids)
        tier_payload = {
            "name": f"Tier-{random.randint(100, 999)}",
            "price": 75.00
        }
        with self.client.post(
            f"/services/{svc_id}/tiers",
            json=tier_payload,
            name="POST /services/:id/tiers (Add Tier)",
            catch_response=True
        ) as resp:
            if resp.status_code in (201, 400, 404):
                resp.success()
            else:
                resp.failure(f"Failed /services/{svc_id}/tiers: {resp.status_code}")


# ============================================================================
# 4. Gateway Health Check (Traffic Weight: 5)
# ============================================================================
class HealthCheckUser(HttpUser):
    """
    Simulates external monitoring and gateway uptime health checks.
    """
    weight = 5
    wait_time = between(1.0, 3.0)

    @tag("health")
    @task
    def health_check(self):
        self.client.get("/", name="GET / (Health Check)")
