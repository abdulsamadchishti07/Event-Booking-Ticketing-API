import enum
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import TIMESTAMP

from .database import Base


# ==========================================================
# ASSOCIATION TABLE: assigns_unit (Bookings <-> InventoryItems)
# Handles M:N seat/unit assignment for 'unit_assigned' mode
# ==========================================================
booking_units = Table(
    "assigns_unit",
    Base.metadata,
    Column(
        "booking_id",
        ForeignKey("bookings.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "inventory_item_id",
        ForeignKey("inventory_items.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


# ==========================================================
# ENTITY: Users
# ==========================================================
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
    otp_expires_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    seller_profile: Mapped[Optional["SellerProfile"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )

    bookings: Mapped[List["Booking"]] = relationship(
        back_populates="user",
    )


# ==========================================================
# ENTITY: SellerProfile
# ==========================================================
class SellerProfile(Base):
    __tablename__ = "seller_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
    )

    business_name: Mapped[str] = mapped_column(String(50))
    business_desc: Mapped[Optional[str]] = mapped_column(String(255))

    is_verified: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )

    user: Mapped["User"] = relationship(back_populates="seller_profile")

    locations: Mapped[List["Location"]] = relationship(
        back_populates="seller",
        cascade="all, delete-orphan",
    )
    services: Mapped[List["Services"]] = relationship(
        back_populates="seller",
        cascade="all, delete-orphan",
    )


# ==========================================================
# ENTITY: Location
# ==========================================================
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


# ==========================================================
# ENTITY: Services & Tiers
# ==========================================================
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
        default=BookingMode.SLOT_CAPACITY,
    )
    max_capacity: Mapped[Optional[int]]
    base_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    seller: Mapped["SellerProfile"] = relationship(back_populates="services")
    location: Mapped["Location"] = relationship(back_populates="services")

    tiers: Mapped[List["ServiceTier"]] = relationship(
        back_populates="service",
        cascade="all, delete-orphan",
    )
    inventory_items: Mapped[List["InventoryItems"]] = relationship(
        back_populates="service",
        cascade="all, delete-orphan",
    )
    bookings: Mapped[List["Booking"]] = relationship(
        back_populates="service"
    )


class ServiceTier(Base):
    __tablename__ = "service_tiers"
    __table_args__ = (
        UniqueConstraint("service_id", "name"),
    )

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
    bookings: Mapped[List["Booking"]] = relationship(back_populates="tier")


# ==========================================================
# ENTITY: InventoryItems
# ==========================================================
class ItemStatus(str, enum.Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    BOOKED = "booked"
    MAINTENANCE = "maintenance"


class InventoryItems(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("service_id", "identifier_code"),
    )

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
        index=True,
    )

    service: Mapped["Services"] = relationship(back_populates="inventory_items")
    tier: Mapped["ServiceTier"] = relationship(back_populates="inventory_items")

    assigned_bookings: Mapped[List["Booking"]] = relationship(
        secondary=booking_units,
        back_populates="assigned_units",
    )


# ==========================================================
# ENTITY: Booking
# ==========================================================
class BookingStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT")
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    tier_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("service_tiers.id", ondelete="RESTRICT"),
        nullable=True,
    )

    quantity: Mapped[int] = mapped_column(default=1)

    start_time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status_enum"),
        default=BookingStatus.PENDING,
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    user: Mapped["User"] = relationship(back_populates="bookings")
    service: Mapped["Services"] = relationship(back_populates="bookings")
    tier: Mapped[Optional["ServiceTier"]] = relationship(back_populates="bookings")

    # M:N to inventory items
    assigned_units: Mapped[List["InventoryItems"]] = relationship(
        secondary=booking_units,
        back_populates="assigned_bookings",
    )

    # 1:1 to Payment
    payment: Mapped[Optional["Payment"]] = relationship(
        back_populates="booking",
        uselist=False,
    )

    # 1:0..1 to Cancellation
    cancellation: Mapped[Optional["Cancellation"]] = relationship(
        back_populates="booking",
        uselist=False,
    )

    # 1:0..1 to Review
    review: Mapped[Optional["Review"]] = relationship(
        back_populates="booking",
        uselist=False,
    )


# ==========================================================
# ENTITY: Payments (Canonical Stripe PaymentIntent)
# ==========================================================
class PaymentStatus(str, enum.Enum):
    REQUIRES_PAYMENT_METHOD = "requires_payment_method"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)

    # 1:1 Relationship with Booking
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )

    stripe_payment_intent_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    client_secret: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd")

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status_enum"),
        default=PaymentStatus.REQUIRES_PAYMENT_METHOD,
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    booking: Mapped["Booking"] = relationship(back_populates="payment")
    invoice: Mapped[Optional["Invoice"]] = relationship(
        back_populates="payment",
        uselist=False,
    )


# ==========================================================
# ENTITY: Invoice
# ==========================================================
class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )

    invoice_number: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    payment: Mapped["Payment"] = relationship(back_populates="invoice")


# ==========================================================
# ENTITY: Cancellation
# ==========================================================
class Cancellation(Base):
    __tablename__ = "cancellations"

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    refund_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    reason: Mapped[Optional[str]] = mapped_column(String(255))
    cancelled_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    booking: Mapped["Booking"] = relationship(back_populates="cancellation")


# ==========================================================
# ENTITY: Review
# ==========================================================
class Review(Base):
    __tablename__ = "reviews"

    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="check_valid_rating_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )

    booking: Mapped["Booking"] = relationship(back_populates="review")