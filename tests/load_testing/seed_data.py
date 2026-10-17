"""
Seed Data Script for High-Throughput Load Testing.
Ensures verified test accounts, locations, events (with high capacity),
seat inventories, and past completed events exist for full-system load testing.
"""

import sys
import os
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
import redis

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.database import SessionLocal, model
from app.core import security as utils
from app.core.config import settings


def seed_all():
    if settings.environment.lower() == "production" or os.getenv("ENVIRONMENT", "").lower() == "production":
        raise RuntimeError(
            "CRITICAL SECURITY GUARD: Refusing to run load-test seed script in a PRODUCTION environment! "
            "This script creates test accounts with known credentials."
        )
    db = SessionLocal()
    print("🌱 Starting Load Test Data Seeding...")

    try:
        # 1. Seed or update Seller Account
        seller_email = "seller@loadtest.com"
        seller = db.query(model.User).filter(model.User.email == seller_email).first()
        if not seller:
            seller = model.User(
                name="LoadTest Organizer",
                email=seller_email,
                password_hash=utils.hash("Password123!"),
                dob=date(1990, 1, 1),
                role=model.UserRole.SELLER,
                phone_no="+923000000000",
                is_verified=True,
            )
            db.add(seller)
            db.commit()
            db.refresh(seller)
            print(f"  ✓ Created seller: {seller_email}")
        else:
            seller.is_verified = True
            seller.role = model.UserRole.SELLER
            seller.password_hash = utils.hash("Password123!")
            db.commit()
            print(f"  ✓ Updated seller: {seller_email}")

        # Ensure Seller Profile exists
        seller_profile = db.query(model.SellerProfile).filter(model.SellerProfile.user_id == seller.id).first()
        if not seller_profile:
            seller_profile = model.SellerProfile(
                user_id=seller.id,
                business_name="Mega Events PK",
                business_desc="Top concert and expo organizer in Pakistan",
                is_verified=True,
            )
            db.add(seller_profile)
            db.commit()
            db.refresh(seller_profile)
            print("  ✓ Created seller profile")

        # 2. Seed 20 Verified Buyers
        for i in range(1, 21):
            b_email = f"buyer{i}@loadtest.com"
            buyer = db.query(model.User).filter(model.User.email == b_email).first()
            if not buyer:
                buyer = model.User(
                    name=f"Buyer User {i}",
                    email=b_email,
                    password_hash=utils.hash("Password123!"),
                    dob=date(1995, 5, 10),
                    role=model.UserRole.CUSTOMER,
                    phone_no=f"+9231{i:08d}",
                    is_verified=True,
                )
                db.add(buyer)
            else:
                buyer.is_verified = True
                buyer.password_hash = utils.hash("Password123!")
        db.commit()
        print("  ✓ Ensured 20 verified buyer accounts (buyer1@loadtest.com .. buyer20@loadtest.com)")

        # 3. Seed Locations
        loc_data = [
            ("Lahore", "Pakistan", "Alhamra Arts Council, Mall Road"),
            ("Islamabad", "Pakistan", "Jinnah Convention Centre, Club Road"),
            ("Karachi", "Pakistan", "Expo Centre, Main University Road"),
        ]
        locations = []
        for city, country, address in loc_data:
            loc = db.query(model.Location).filter(
                model.Location.city == city,
                model.Location.country == country,
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
        print(f"  ✓ Ensured {len(locations)} venue locations in {', '.join(l.city for l in locations)}")

        now = datetime.now(timezone.utc)

        # 4. Seed Slot-Capacity Event (High Capacity for high concurrency booking)
        slot_event = db.query(model.Services).filter(model.Services.service_name == "Global Tech Summit 2026").first()
        if not slot_event:
            slot_event = model.Services(
                seller_id=seller_profile.id,
                location_id=locations[1].id,
                service_name="Global Tech Summit 2026",
                service_desc="Largest technology leadership and AI summit in South Asia.",
                booking_mode=model.BookingMode.SLOT_CAPACITY,
                max_capacity=50000,
                base_price=Decimal("35.00"),
                start_time=now + timedelta(days=20),
                end_time=now + timedelta(days=21),
            )
            db.add(slot_event)
            db.commit()
            db.refresh(slot_event)
        else:
            # Refresh future dates & capacity
            slot_event.start_time = now + timedelta(days=20)
            slot_event.end_time = now + timedelta(days=21)
            slot_event.max_capacity = 50000
            db.commit()
            db.refresh(slot_event)

        # Ensure Tier for Slot Event
        slot_tier = db.query(model.ServiceTier).filter(
            model.ServiceTier.service_id == slot_event.id,
            model.ServiceTier.name == "Standard Delegate"
        ).first()
        if not slot_tier:
            slot_tier = model.ServiceTier(
                service_id=slot_event.id,
                name="Standard Delegate",
                price=Decimal("35.00")
            )
            db.add(slot_tier)
            db.commit()
            db.refresh(slot_tier)
        print(f"  ✓ Slot-Capacity Event ready: ID={slot_event.id} ('{slot_event.service_name}', Capacity={slot_event.max_capacity})")

        # 5. Seed Unit-Assigned Event (Reserved Seating)
        unit_event = db.query(model.Services).filter(model.Services.service_name == "Live Music Festival 2026").first()
        if not unit_event:
            unit_event = model.Services(
                seller_id=seller_profile.id,
                location_id=locations[0].id,
                service_name="Live Music Festival 2026",
                service_desc="Grand open-air musical festival featuring top classical and modern bands.",
                booking_mode=model.BookingMode.UNIT_ASSIGNED,
                base_price=Decimal("50.00"),
                start_time=now + timedelta(days=14),
                end_time=now + timedelta(days=14, hours=5),
            )
            db.add(unit_event)
            db.commit()
            db.refresh(unit_event)
        else:
            unit_event.start_time = now + timedelta(days=14)
            unit_event.end_time = now + timedelta(days=14, hours=5)
            db.commit()
            db.refresh(unit_event)

        # Ensure Tiers for Unit Event
        vip_tier = db.query(model.ServiceTier).filter(
            model.ServiceTier.service_id == unit_event.id,
            model.ServiceTier.name == "VIP"
        ).first()
        if not vip_tier:
            vip_tier = model.ServiceTier(
                service_id=unit_event.id,
                name="VIP",
                price=Decimal("150.00")
            )
            db.add(vip_tier)
            db.commit()
            db.refresh(vip_tier)

        gen_tier = db.query(model.ServiceTier).filter(
            model.ServiceTier.service_id == unit_event.id,
            model.ServiceTier.name == "General"
        ).first()
        if not gen_tier:
            gen_tier = model.ServiceTier(
                service_id=unit_event.id,
                name="General",
                price=Decimal("50.00")
            )
            db.add(gen_tier)
            db.commit()
            db.refresh(gen_tier)

        # Ensure Inventory Seats
        current_seats_count = db.query(model.InventoryItems).filter(
            model.InventoryItems.service_id == unit_event.id
        ).count()
        if current_seats_count < 100:
            for s_idx in range(1, 51):
                vip_seat = model.InventoryItems(
                    service_id=unit_event.id,
                    tier_id=vip_tier.id,
                    identifier_code=f"VIP-{s_idx:02d}",
                    status=model.ItemStatus.AVAILABLE
                )
                db.add(vip_seat)

                gen_seat = model.InventoryItems(
                    service_id=unit_event.id,
                    tier_id=gen_tier.id,
                    identifier_code=f"GEN-{s_idx:02d}",
                    status=model.ItemStatus.AVAILABLE
                )
                db.add(gen_seat)
            db.commit()
        else:
            # Reset any reserved seats back to available for fresh benchmark runs
            db.query(model.InventoryItems).filter(
                model.InventoryItems.service_id == unit_event.id
            ).update({"status": model.ItemStatus.AVAILABLE})
            db.commit()
        print(f"  ✓ Unit-Assigned Event ready: ID={unit_event.id} ('{unit_event.service_name}', 100 seats available)")

        # 6. Seed Completed Event with Confirmed Booking (For Review Testing)
        past_event = db.query(model.Services).filter(model.Services.service_name == "Spring Jazz Night (Completed)").first()
        if not past_event:
            past_event = model.Services(
                seller_id=seller_profile.id,
                location_id=locations[0].id,
                service_name="Spring Jazz Night (Completed)",
                service_desc="Intimate evening of acoustic jazz in Lahore.",
                booking_mode=model.BookingMode.SLOT_CAPACITY,
                max_capacity=200,
                base_price=Decimal("25.00"),
                start_time=now - timedelta(days=7),
                end_time=now - timedelta(days=7, hours=-3),
            )
            db.add(past_event)
            db.commit()
            db.refresh(past_event)

        first_buyer = db.query(model.User).filter(model.User.email == "buyer1@loadtest.com").first()
        past_booking = db.query(model.Booking).filter(
            model.Booking.service_id == past_event.id,
            model.Booking.user_id == first_buyer.id
        ).first()
        if not past_booking:
            past_booking = model.Booking(
                user_id=first_buyer.id,
                service_id=past_event.id,
                quantity=1,
                start_time=past_event.start_time,
                end_time=past_event.end_time,
                status=model.BookingStatus.CONFIRMED,
            )
            db.add(past_booking)
            db.commit()
            db.refresh(past_booking)

        existing_review = db.query(model.Review).filter(model.Review.booking_id == past_booking.id).first()
        if not existing_review:
            review = model.Review(
                booking_id=past_booking.id,
                rating=5,
                comment="Incredible musical performance, crystal clear acoustic sound!"
            )
            db.add(review)
            db.commit()
        print(f"  ✓ Past Event & Review ready: ID={past_event.id} ('{past_event.service_name}')")

        # 7. Flush Redis Cache & Rate Limits
        try:
            r = redis.from_url(settings.redis_url, decode_responses=True)
            r.flushdb()
            print("  ✓ Flushed Redis Cache and Rate Limits successfully.")
        except Exception as e:
            print(f"  ⚠ Redis flush notice: {e}")

        print("✨ Load test database seeding complete!\n")

    except Exception as e:
        db.rollback()
        print(f"❌ Error during seeding: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_all()
