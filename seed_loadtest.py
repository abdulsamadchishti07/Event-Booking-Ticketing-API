#!/usr/bin/env python3
"""
seed_loadtest.py

High-performance seed script for k6 concurrency and capacity benchmarks.
Generates:
  - 5,000 verified users with pre-minted 3-hour JWT access tokens
  - 300 active events across multiple locations
  - 1 unit-assigned "drop" event with 500 individual seat inventory items
  - 1 large general-admission "big" event for high-concurrency booking holds
  - Writes 'loadctx.json' in the current working directory for k6
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# Ensure project root is in python module search path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.core import oauth2, security as utils
from app.core.config import settings
from app.database import SessionLocal, model


def seed_loadtest(num_users: int = 5000):
    env = os.getenv("ENVIRONMENT", settings.environment).lower()
    if env == "production":
        print("❌ CRITICAL: Cannot run seed_loadtest in production environment!")
        sys.exit(1)

    print(f"🌱 [1/5] Initializing load test database seeding ({num_users} users)...")
    db = SessionLocal()

    try:
        # ======================================================================
        # 1. Seed or Retrieve Seller & Seller Profile
        # ======================================================================
        seller_email = "organizer@loadtest.internal"
        seller = db.query(model.User).filter(model.User.email == seller_email).first()
        if not seller:
            seller = model.User(
                name="LoadTest Event Organizer",
                email=seller_email,
                password_hash=utils.hash("OrganizerPass123!"),
                dob=date(1990, 1, 1),
                role=model.UserRole.SELLER,
                phone_no="+923000000001",
                is_verified=True,
                is_active=True
            )
            db.add(seller)
            db.commit()
            db.refresh(seller)

        seller_profile = db.query(model.SellerProfile).filter(
            model.SellerProfile.user_id == seller.id
        ).first()
        if not seller_profile:
            seller_profile = model.SellerProfile(
                user_id=seller.id,
                business_name="National Arena & Concerts Org",
                business_desc="Official organizer for high-throughput load test events",
                is_verified=True
            )
            db.add(seller_profile)
            db.commit()
            db.refresh(seller_profile)

        print(f"  ✓ Seller profile active: {seller_profile.business_name} (ID: {seller_profile.id})")

        # ======================================================================
        # 2. Seed Locations
        # ======================================================================
        cities = [
            ("Lahore", "Pakistan", "Gaddafi Stadium Arena"),
            ("Karachi", "Pakistan", "National Bank Cricket Arena"),
            ("Islamabad", "Pakistan", "Jinnah Sports Stadium"),
            ("Rawalpindi", "Pakistan", "Rawalpindi Stadium"),
            ("Peshawar", "Pakistan", "Arbab Niaz Complex"),
            ("Multan", "Pakistan", "Multan Cricket Stadium"),
            ("Faisalabad", "Pakistan", "Iqbal Stadium"),
            ("Quetta", "Pakistan", "Bugti Stadium Arena"),
            ("Sialkot", "Pakistan", "Jinnah Sports Complex"),
            ("Hyderabad", "Pakistan", "Niaz Stadium")
        ]

        locations = []
        for city, country, address in cities:
            loc = db.query(model.Location).filter(
                model.Location.city == city,
                model.Location.seller_id == seller_profile.id
            ).first()
            if not loc:
                loc = model.Location(
                    seller_id=seller_profile.id,
                    city=city,
                    country=country,
                    address_line=address
                )
                db.add(loc)
                db.commit()
                db.refresh(loc)
            locations.append(loc)

        print(f"  ✓ {len(locations)} tournament locations ready")

        # ======================================================================
        # 3. Seed 300 Events + Drop Event + Big Event
        # ======================================================================
        now = datetime.now(timezone.utc)
        print("🌱 [2/5] Seeding 300 discovery events, 1 drop event, and 1 big event...")

        existing_events_count = db.query(model.Services).filter(
            model.Services.seller_id == seller_profile.id,
            model.Services.is_active == True
        ).count()

        event_ids = []
        if existing_events_count < 300:
            events_to_create = 300 - existing_events_count
            new_events = []
            for i in range(events_to_create):
                idx = existing_events_count + i + 1
                loc = locations[i % len(locations)]
                start_time = now + timedelta(days=2 + (i % 30), hours=(i % 12))
                end_time = start_time + timedelta(hours=3)
                price = Decimal(f"{(10 + (i % 40)):.2f}")

                srv = model.Services(
                    seller_id=seller_profile.id,
                    location_id=loc.id,
                    service_name=f"Championship Match #{idx:03d} - {loc.city}",
                    booking_mode=model.BookingMode.SLOT_CAPACITY,
                    max_capacity=5000,
                    base_price=price,
                    start_time=start_time,
                    end_time=end_time,
                    is_active=True
                )
                new_events.append(srv)

            db.add_all(new_events)
            db.commit()

        # Collect at least 300 event IDs
        event_rows = db.query(model.Services.id).filter(
            model.Services.seller_id == seller_profile.id,
            model.Services.is_active == True
        ).limit(300).all()
        event_ids = [r[0] for r in event_rows]

        # 3a. The "big" general admission event
        big_event = db.query(model.Services).filter(
            model.Services.service_name == "Grand Festival Main Stage (Unlimited)"
        ).first()
        if not big_event:
            big_event = model.Services(
                seller_id=seller_profile.id,
                location_id=locations[0].id,
                service_name="Grand Festival Main Stage (Unlimited)",
                booking_mode=model.BookingMode.SLOT_CAPACITY,
                max_capacity=1000000,
                base_price=Decimal("25.00"),
                start_time=now + timedelta(days=7),
                end_time=now + timedelta(days=7, hours=6),
                is_active=True
            )
            db.add(big_event)
            db.commit()
            db.refresh(big_event)
        big_id = big_event.id

        # 3b. The "drop" 500-seat unit-assigned event
        drop_event = db.query(model.Services).filter(
            model.Services.service_name == "Flash Ticket Drop Arena (500 Seats)"
        ).first()
        if not drop_event:
            drop_event = model.Services(
                seller_id=seller_profile.id,
                location_id=locations[1].id,
                service_name="Flash Ticket Drop Arena (500 Seats)",
                booking_mode=model.BookingMode.UNIT_ASSIGNED,
                base_price=Decimal("50.00"),
                start_time=now + timedelta(days=5),
                end_time=now + timedelta(days=5, hours=4),
                is_active=True
            )
            db.add(drop_event)
            db.commit()
            db.refresh(drop_event)

        drop_id = drop_event.id

        # Ensure tier exists for drop event
        drop_tier = db.query(model.ServiceTier).filter(
            model.ServiceTier.service_id == drop_id
        ).first()
        if not drop_tier:
            drop_tier = model.ServiceTier(
                service_id=drop_id,
                name="Front Row VIP",
                price=Decimal("50.00")
            )
            db.add(drop_tier)
            db.commit()
            db.refresh(drop_tier)

        # Ensure 500 seats exist and are AVAILABLE
        existing_seats = db.query(model.InventoryItems).filter(
            model.InventoryItems.service_id == drop_id
        ).all()

        if len(existing_seats) < 500:
            # Delete any partial/stale seats to recreate a clean set of 500 seats
            db.query(model.InventoryItems).filter(
                model.InventoryItems.service_id == drop_id
            ).delete()
            db.commit()

            seat_objects = [
                model.InventoryItems(
                    service_id=drop_id,
                    tier_id=drop_tier.id,
                    identifier_code=f"SEAT-{s:03d}",
                    status=model.ItemStatus.AVAILABLE
                )
                for s in range(1, 501)
            ]
            db.bulk_save_objects(seat_objects)
            db.commit()

            existing_seats = db.query(model.InventoryItems).filter(
                model.InventoryItems.service_id == drop_id
            ).order_by(model.InventoryItems.id.asc()).all()
        else:
            # Reset existing seats to AVAILABLE in case of previous run
            db.query(model.InventoryItems).filter(
                model.InventoryItems.service_id == drop_id
            ).update({model.InventoryItems.status: model.ItemStatus.AVAILABLE})
            db.commit()

        seat_ids = [s.id for s in existing_seats[:500]]
        print(f"  ✓ Drop event ID {drop_id} with {len(seat_ids)} seats ready")
        print(f"  ✓ Big event ID {big_id} ready")

        # ======================================================================
        # 4. Seed Users in Fast Batches & Generate Tokens
        # ======================================================================
        print(f"🌱 [3/5] Seeding {num_users} users with pre-computed Argon2id hash...")
        # Precompute Argon2id hash ONCE so we don't block CPU on 5,000 hashes
        fixed_pwd_hash = utils.hash("UserLoadTestPass123!")

        existing_user_count = db.query(model.User).filter(
            model.User.email.like("k6_user_%@loadtest.internal")
        ).count()

        if existing_user_count < num_users:
            needed = num_users - existing_user_count
            print(f"  Creating {needed} additional loadtest user accounts...")
            batch_size = 500
            for start in range(0, needed, batch_size):
                batch_users = []
                for j in range(start, min(start + batch_size, needed)):
                    u_idx = existing_user_count + j + 1
                    batch_users.append(
                        model.User(
                            name=f"Load Tester #{u_idx}",
                            email=f"k6_user_{u_idx:05d}@loadtest.internal",
                            password_hash=fixed_pwd_hash,
                            dob=date(1995, 1, 1),
                            role=model.UserRole.CUSTOMER,
                            phone_no=f"+924{u_idx:08d}",
                            is_verified=True,
                            is_active=True
                        )
                    )
                db.bulk_save_objects(batch_users)
                db.commit()
                print(f"    - Seeded batch {min(start + batch_size, needed)} / {needed}")

        # Retrieve user IDs to mint JWT tokens
        user_rows = db.query(model.User.id).filter(
            model.User.email.like("k6_user_%@loadtest.internal")
        ).order_by(model.User.id.asc()).limit(num_users).all()

        user_ids = [r[0] for r in user_rows]

        # ======================================================================
        # 5. Mint 3-Hour JWT Access Tokens
        # ======================================================================
        print(f"🌱 [4/5] Minting {len(user_ids)} JWT tokens (3-hour expiry)...")
        token_expires = timedelta(hours=3)
        tokens = [
            oauth2.create_access_token(
                data={"sub": str(uid), "user_id": uid},
                expires_delta=token_expires
            )
            for uid in user_ids
        ]

        # ======================================================================
        # 6. Write 'loadctx.json'
        # ======================================================================
        ctx_data = {
            "tokens": tokens,
            "event_ids": event_ids,
            "drop": drop_id,
            "seat_ids": seat_ids,
            "big": big_id
        }

        output_path = os.path.abspath("loadctx.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(ctx_data, f)

        # Also copy loadctx.json to tests/load_testing/loadctx.json for convenience
        test_dir_ctx = os.path.abspath("tests/load_testing/loadctx.json")
        with open(test_dir_ctx, "w", encoding="utf-8") as f:
            json.dump(ctx_data, f)

        print(f"🌱 [5/5] Successfully generated {output_path} ({len(tokens)} tokens, {len(event_ids)} events, {len(seat_ids)} drop seats)")
        print("\n✅ Ready to benchmark! Run:")
        print("  k6 run -e TARGET=http://localhost -e BROWSE_RPS=100 -e BOOK_RPS=10 -e DROP_USERS=300 -e HOLD=1m tests/load_testing/loadtest.js")

    finally:
        db.close()


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    seed_loadtest(count)
