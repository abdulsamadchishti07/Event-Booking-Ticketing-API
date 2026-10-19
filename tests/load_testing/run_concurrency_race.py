"""
High-concurrency race test runner (Python / HTTPX Async).
Fires N simultaneous booking requests against the live server for M scarce tickets.
Asserts the fundamental ticketing invariant: ZERO OVERSOLD TICKETS.
"""

import argparse
import asyncio
from datetime import datetime
import json
import os
import sys
import time
from typing import Dict, List

import httpx

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

from app.database import SessionLocal, model


async def buyer_request(
    client: httpx.AsyncClient,
    barrier: asyncio.Barrier,
    target_url: str,
    service_id: int,
    buyer: dict,
    spoof_ip: bool = True
) -> Dict:
    buyer_idx = buyer["buyer_index"]
    token = buyer["token"]
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    if spoof_ip:
        # Give each virtual buyer a simulated distinct IP
        headers["X-Forwarded-For"] = f"198.51.100.{buyer_idx % 250 + 1}"

    payload = {
        "service_id": service_id,
        "quantity": 1
    }

    # Synchronize all buyer tasks at the barrier so they fire simultaneously
    await barrier.wait()

    start_time = time.perf_counter()
    try:
        response = await client.post(
            f"{target_url}/bookings",
            json=payload,
            headers=headers,
            timeout=15.0
        )
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        return {
            "buyer_index": buyer_idx,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "body": response.text[:200]
        }
    except Exception as e:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        return {
            "buyer_index": buyer_idx,
            "status_code": 0,
            "latency_ms": latency_ms,
            "error": str(e)
        }


async def main_async(target_url: str, spoof_ip: bool):
    context_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drop_test_context.json")
    if not os.path.exists(context_file):
        print(f"❌ Context file missing: {context_file}")
        print("Please run `uv run python tests/load_testing/seed_concurrency_drop.py` first.")
        sys.exit(1)

    with open(context_file, "r") as f:
        context = json.load(f)

    service_id = context["service_id"]
    max_capacity = context["max_capacity"]
    buyers = context["buyers"]
    num_buyers = len(buyers)

    print("\n" + "=" * 80)
    print(f"  CONCURRENCY DROP RACE: {num_buyers} BUYERS RACING FOR {max_capacity} TICKETS")
    print("=" * 80)
    print(f"  Target Server:       {target_url}")
    print(f"  Target Event ID:     {service_id}")
    print(f"  Ticket Capacity:     {max_capacity} seats")
    print(f"  Concurrent Buyers:   {num_buyers} users")
    print(f"  Spoof Remote IP:     {spoof_ip}")
    print("-" * 80)
    print("  Synchronizing all virtual buyers at the gate...")

    barrier = asyncio.Barrier(num_buyers)

    # Configure client connection pool to allow all concurrent requests without local queueing
    limits = httpx.Limits(max_keepalive_connections=num_buyers + 10, max_connections=num_buyers + 20)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [
            buyer_request(client, barrier, target_url, service_id, buyer, spoof_ip)
            for buyer in buyers
        ]
        print("  ⚡ FIRING SIMULTANEOUS DROP REQUESTS...")
        t_start = time.perf_counter()
        results = await asyncio.gather(*tasks)
        total_wall_time = time.perf_counter() - t_start

    # Tally results
    status_counts = {}
    latencies = []
    won_buyers = []
    conflicted_buyers = []
    error_buyers = []

    for r in results:
        code = r["status_code"]
        status_counts[code] = status_counts.get(code, 0) + 1
        latencies.append(r["latency_ms"])

        if code == 201:
            won_buyers.append(r["buyer_index"])
        elif code == 409:
            conflicted_buyers.append(r["buyer_index"])
        else:
            error_buyers.append(r)

    latencies.sort()
    med_lat = latencies[len(latencies) // 2]
    p95_lat = latencies[int(len(latencies) * 0.95)]
    p99_lat = latencies[int(len(latencies) * 0.99)]
    max_lat = max(latencies)

    # Database state verification
    with SessionLocal() as db:
        actual_db_bookings = db.query(model.Booking).filter(
            model.Booking.service_id == service_id,
            model.Booking.status == model.BookingStatus.PENDING
        ).count()

    print("\n" + "=" * 80)
    print("  CONCURRENCY DROP RESULTS SUMMARY")
    print("=" * 80)
    print(f"  All Status Counts:       {status_counts}")
    if results:
        print(f"  Sample Response [0]:     Code {results[0]['status_code']} -> {results[0].get('body') or results[0].get('error')}")
    print(f"  HTTP 201 Created (Won):  {status_counts.get(201, 0):>3} (Expected: {max_capacity})")
    print(f"  HTTP 409 Conflict:       {status_counts.get(409, 0):>3} (Expected: {num_buyers - max_capacity})")
    print(f"  HTTP 429 Rate Limited:   {status_counts.get(429, 0):>3}")
    print(f"  HTTP 500 / 503 Errors:   {status_counts.get(500, 0) + status_counts.get(503, 0):>3}")
    if status_counts.get(0, 0) > 0:
        print(f"  Connection Timeouts:     {status_counts.get(0, 0):>3}")
    print("-" * 80)
    print("  LATENCY METRICS (Round-Trip):")
    print(f"  Median: {med_lat:.1f} ms | p95: {p95_lat:.1f} ms | p99: {p99_lat:.1f} ms | Max: {max_lat:.1f} ms")
    print("-" * 80)
    print("  DATABASE INVENTORY AUDIT:")
    print(f"  Capacity Defined:        {max_capacity}")
    print(f"  Pending Holds in DB:     {actual_db_bookings}")
    print(f"  Oversold Tickets:        {max(0, actual_db_bookings - max_capacity)}")
    print("=" * 80)

    # Invariant checks
    success_pass = (status_counts.get(201, 0) == max_capacity)
    conflict_pass = (status_counts.get(409, 0) == (num_buyers - max_capacity))
    oversold_pass = (actual_db_bookings == max_capacity)
    no_server_errors = (status_counts.get(500, 0) == 0 and status_counts.get(503, 0) == 0 and status_counts.get(0, 0) == 0)

    if success_pass and conflict_pass and oversold_pass and no_server_errors:
        print("\n  🏆 VERDICT: PASSED ✅ ZERO OVERSOLD TICKETS (100% ACID INVARIANT GUARANTEED)\n")
        sys.exit(0)
    else:
        print("\n  ❌ VERDICT: FAILED")
        if not oversold_pass:
            print(f"     - Database inventory violation: {actual_db_bookings} held vs {max_capacity} allowed!")
        if not no_server_errors:
            print(f"     - Server errors encountered: {error_buyers[:3]}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Run concurrency race test against ticketing API")
    parser.add_argument("--target", type=str, default="http://localhost", help="API base URL (default: http://localhost)")
    parser.add_argument("--no-spoof-ip", action="store_true", help="Do not send simulated X-Forwarded-For headers")
    args = parser.parse_args()

    asyncio.run(main_async(args.target, spoof_ip=not args.no_spoof_ip))


if __name__ == "__main__":
    main()
