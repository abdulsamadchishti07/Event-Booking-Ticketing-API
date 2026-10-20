"""
Seed script for concurrency & correctness testing.
Creates a controlled test event with M tickets and N pre-authenticated buyer accounts.
Exports tests/load_testing/drop_test_context.json for use by test runners.
"""

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys

from sqlalchemy import or_

# Ensure repository root is in Python module path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
BASE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR)) if "__file__" in globals() else os.getcwd()
sys.path.insert(0, BASE_DIR)

from app.core import oauth2, utils
from app.database import SessionLocal, model


def seed_drop_test(capacity: int = 20, num_users: int = 100):
    print(f"\n🌱 Initializing Concurrency Test Data: {num_users} buyers racing for {capacity} tickets...")
    
    with SessionLocal() as db:
        # 1. Clean up previous test drop data and test buyers
        old_services = db.query(model.Services).filter(
            model.Services.service_name.like("[CONCURRENCY_TEST]%")
        ).all()
        for s in old_services:
            db.query(model.Booking).filter(model.Booking.service_id == s.id).delete()
            db.delete(s)
        db.commit()

        # Delete old test buyers to ensure clean slate
        db.query(model.User).filter(
            or_(
                model.User.email.like("drop_buyer_%"),
                model.User.phone_no.like("+17000000%")
            )
        ).delete(synchronize_session=False)
        db.commit()

        # 2. Find or create a seller
        seller_user = db.query(model.User).filter(
            or_(model.User.email == "drop_seller@example.com", model.User.phone_no == "+18005550199")
        ).first()
        if not seller_user:
            seller_user = model.User(
                name="Drop Test Seller",
                email="drop_seller@example.com",
                password_hash=utils.hash("TestPass123!"),
                dob=date(1990, 1, 1),
                role=model.UserRole.SELLER,
                phone_no="+18005550199",
                is_verified=True,
                is_active=True
            )
            db.add(seller_user)
            db.commit()
            db.refresh(seller_user)
        else:
            seller_user.email = "drop_seller@example.com"
            seller_user.is_active = True
            seller_user.is_verified = True
            db.commit()

        seller_profile = db.query(model.SellerProfile).filter(model.SellerProfile.user_id == seller_user.id).first()
        if not seller_profile:
            seller_profile = model.SellerProfile(
                user_id=seller_user.id,
                business_name="Concurrency Arena Events",
                is_verified=True
            )
            db.add(seller_profile)
            db.commit()
            db.refresh(seller_profile)

        # 3. Find or create a location
        location = db.query(model.Location).filter(
            model.Location.seller_id == seller_profile.id,
            model.Location.city == "LoadTest City"
        ).first()
        if not location:
            location = model.Location(
                seller_id=seller_profile.id,
                city="LoadTest City",
                country="US",
                address_line="100 Arena Way"
            )
            db.add(location)
            db.commit()
            db.refresh(location)

        # 4. Create the target test event with exact capacity
        now = datetime.now(timezone.utc)
        test_event = model.Services(
            seller_id=seller_profile.id,
            location_id=location.id,
            service_name="[CONCURRENCY_TEST] Flash Sale Arena Drop",
            service_desc=f"Concurrency test event with strictly {capacity} available tickets.",
            booking_mode=model.BookingMode.SLOT_CAPACITY,
            max_capacity=capacity,
            base_price=Decimal("45.00"),
            start_time=now + timedelta(days=14),
            end_time=now + timedelta(days=14, hours=4),
            is_active=True
        )
        db.add(test_event)
        db.commit()
        db.refresh(test_event)

        print(f"✅ Created test event: ID {test_event.id} | Mode: SLOT_CAPACITY | Max Capacity: {test_event.max_capacity}")

        # 5. Create test buyer users and generate pre-signed JWT tokens
        dummy_hash = utils.hash("BuyerPass123!")
        buyers = [
            model.User(
                name=f"Drop Buyer {i}",
                email=f"drop_buyer_{i}@example.com",
                password_hash=dummy_hash,
                dob=date(1995, 5, 20),
                role=model.UserRole.CUSTOMER,
                phone_no=f"+170{i:08d}"[-12:],
                is_verified=True,
                is_active=True
            )
            for i in range(1, num_users + 1)
        ]
        db.add_all(buyers)
        db.commit()
        for u in buyers:
            db.refresh(u)

        token_ttl = timedelta(days=7)
        buyers_data = [
            {
                "buyer_index": i + 1,
                "user_id": u.id,
                "email": u.email,
                "token": oauth2.create_access_token(
                    data={"sub": str(u.id), "user_id": u.id},
                    expires_delta=token_ttl
                )
            }
            for i, u in enumerate(buyers)
        ]

        # 6. Save context to drop_test_context.json
        output_dir = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
        output_path = os.path.join(output_dir, "drop_test_context.json")
        
        context = {
            "service_id": test_event.id,
            "max_capacity": capacity,
            "num_buyers": len(buyers_data),
            "generated_at": now.isoformat(),
            "buyers": buyers_data
        }

        with open(output_path, "w") as f:
            json.dump(context, f, indent=2)

        print(f"✅ Generated {len(buyers_data)} authenticated buyer tokens")
        print(f"📁 Exported context to: {output_path}")
        print("\n🚀 Ready for race testing!")
        print(f"   Python Runner: uv run python tests/load_testing/run_concurrency_race.py")
        print(f"   k6 Runner:     docker run --rm -i --network=host -v \"$PWD/tests/load_testing:/scripts\" grafana/k6 run /scripts/k6_concurrency_race.js\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed concurrency drop test event & buyers")
    parser.add_argument("--capacity", type=int, default=20, help="Total available tickets (default: 20)")
    parser.add_argument("--users", type=int, default=100, help="Number of concurrent buyers (default: 100)")
    args = parser.parse_args()

    seed_drop_test(capacity=args.capacity, num_users=args.users)
