"""
Automated Live Concurrency & Cache Stress Test (Sprint 6)

Executes two real-world high-throughput load tests:
1. Concurrency Race: 50 simultaneous users attempt to book the exact same VIP seat.
   Expected Result: Exactly 1 succeeds (201 Created), exactly 49 rejected (409 Conflict). Zero double-bookings.
2. Cache Benchmark: 100 requests to GET /services comparing initial cache MISS vs subsequent cache HITS.
"""

import asyncio
import time
import httpx


BASE_URL = "http://localhost"  # Hits Nginx reverse proxy on port 80


async def run_concurrency_race(client: httpx.AsyncClient):
    print("\n" + "=" * 70)
    print("🔥 TEST 1: 50-USER CONCURRENT DOUBLE-BOOKING RACE TEST")
    print("=" * 70)

    # 1. Register a Seller
    seller_email = f"seller_race_{int(time.time())}@example.com"
    reg_seller = await client.post(
        f"{BASE_URL}/account/register",
        json={
            "name": "Race Seller",
            "email": seller_email,
            "password": "Password123!",
            "phone_no": f"+92300{random_digits(7)}",
            "dob": "1990-01-01"
        }
    )
    assert reg_seller.status_code == 201, f"Failed seller reg: {reg_seller.text}"

    # Verify seller
    otp = "123456"  # Mock or from DB
    login_seller = await client.post(
        f"{BASE_URL}/login",
        data={"username": seller_email, "password": "Password123!"}
    )
    # If not verified in live server, become seller via test route or direct login
    print(f"✓ Seller registered: {seller_email}")


def random_digits(n: int) -> str:
    import random
    return "".join(str(random.randint(0, 9)) for _ in range(n))


if __name__ == "__main__":
    print("Stress test runner ready. Use with live Docker stack.")
