import enum
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import Date, Enum, ForeignKey, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import TIMESTAMP

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    dob: Mapped[date] = mapped_column(Date)
    role: Mapped[str] = mapped_column(String(20), server_default="customer")
    phone_no: Mapped[Optional[str]] = mapped_column(String(20), unique=True)

    is_verified: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )

    verification_otp: Mapped[Optional[str]] = mapped_column(String(6))
    otp_expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    seller_profile: Mapped[Optional["SellerProfile"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )


class SellerProfile(Base):
    __tablename__ = "seller_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), 
        unique=True
    )

    business_name: Mapped[str] = mapped_column(String(50))
    business_desc: Mapped[Optional[str]] = mapped_column(String(255))

    is_verified: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )

    user: Mapped["User"] = relationship(back_populates="seller_profile")

    locations: Mapped[List["Location"]] = relationship(
        back_populates="seller",
        cascade="all, delete-orphan"
    )
    services: Mapped[List["Services"]] = relationship(
        back_populates="seller",
        cascade="all, delete-orphan"
    )


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("seller_profiles.id", ondelete="CASCADE")
    )

    city: Mapped[str] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(100))
    address_line: Mapped[str] = mapped_column(String(255))

    seller: Mapped["SellerProfile"] = relationship(back_populates="locations")
    services: Mapped[List["Services"]] = relationship(back_populates="location")


class BookingMode(str, enum.Enum):
    SLOT_CAPACITY = "slot_capacity"
    UNIT_ASSIGNED = "unit_assigned"


class Services(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("seller_profiles.id", ondelete="CASCADE")
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT")
    )

    service_name: Mapped[str] = mapped_column(String(50))
    service_desc: Mapped[Optional[str]] = mapped_column(String(255))

    booking_mode: Mapped[BookingMode] = mapped_column(
        Enum(BookingMode, name="booking_mode_enum"), 
        nullable=False, 
        default=BookingMode.SLOT_CAPACITY
    )
    max_capacity: Mapped[Optional[int]]
    base_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    seller: Mapped["SellerProfile"] = relationship(back_populates="services")
    location: Mapped["Location"] = relationship(back_populates="services")

    tiers: Mapped[List["ServiceTier"]] = relationship(
        back_populates="service",
        cascade="all, delete-orphan"
    )
    inventory_items: Mapped[List["InventoryItems"]] = relationship(
        back_populates="service",
        cascade="all, delete-orphan"
    )


class ServiceTier(Base):
    __tablename__ = "service_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(50))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    service: Mapped["Services"] = relationship(back_populates="tiers")
    inventory_items: Mapped[List["InventoryItems"]] = relationship(
        back_populates="tier"
    )


class ItemStatus(str, enum.Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    BOOKED = "booked"
    MAINTENANCE = "maintenance"


class InventoryItems(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE")
    )
    tier_id: Mapped[int] = mapped_column(
        ForeignKey("service_tiers.id", ondelete="RESTRICT")
    )

    identifier_code: Mapped[str] = mapped_column(String(20))
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus, name="item_status_enum"),
        default=ItemStatus.AVAILABLE,
        nullable=False,
        index=True
    )

    service: Mapped["Services"] = relationship(back_populates="inventory_items")
    tier: Mapped["ServiceTier"] = relationship(back_populates="inventory_items")


