import random
from locust import HttpUser, task, between, tag


class EventDiscoveryUser(HttpUser):
    """
    Simulates high-throughput public traffic browsing events and seat maps.
    Measures sub-5ms Redis cache HIT latency versus initial database MISS.
    """
    wait_time = between(0.1, 0.5)

    @tag("discovery")
    @task(5)
    def browse_services_list(self):
        # Hits cached search queries with standard parameters
        params = random.choice([
            {"city": "Lahore"},
            {"city": "Islamabad"},
            {"sort_by": "date_asc"},
            {"limit": 20},
            {}
        ])
        with self.client.get("/services", params=params, name="/services (Search & Filter)", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Unexpected status code: {resp.status_code}")

    @tag("discovery")
    @task(3)
    def view_service_detail(self):
        # Hits specific service detail (cached)
        with self.client.get("/services/1", name="/services/:id (Event Detail)", catch_response=True) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            else:
                resp.failure(f"Failed with status: {resp.status_code}")

    @tag("discovery")
    @task(2)
    def view_seat_map(self):
        # Hits live seat map (cached)
        with self.client.get("/services/1/seats", name="/services/:id/seats (Seat Map)", catch_response=True) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            else:
                resp.failure(f"Failed with status: {resp.status_code}")


class HealthCheckUser(HttpUser):
    """
    Simulates gateway health check probes routed through Nginx.
    """
    wait_time = between(1.0, 3.0)

    @tag("health")
    @task
    def health_check(self):
        self.client.get("/", name="/ (Health Check)")
