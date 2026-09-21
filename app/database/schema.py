from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ..database.model import (
    BookingMode,
    BookingStatus,
    ItemStatus,
    PaymentStatus,
    UserRole,
)


# ==========================================================
# Generic & Response Schemas
# ==========================================================
class MessageResponse(BaseModel):
    message: str


# ==========================================================
# Authentication & Verification Schemas
# ==========================================================
class VerifyOTP(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6, description="6-digit verification OTP")


class ResendOTP(BaseModel):
    email: EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[int] = None
    email: Optional[str] = None
    role: Optional[UserRole] = None

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6, description="6-digit reset code")
    new_password: str = Field(..., min_length=6, max_length=100, description="New password")



# ==========================================================
# 1. User Schemas
# ==========================================================
class UserBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    email: EmailStr
    dob: date
    phone_no: Optional[str] = Field(None, max_length=20)
    role: Optional[UserRole] = Field(default=UserRole.CUSTOMER)


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, max_length=100)


class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    email: Optional[EmailStr] = None
    dob: Optional[date] = None
    phone_no: Optional[str] = Field(None, max_length=20)
    role: Optional[UserRole] = None
    password: Optional[str] = Field(None, min_length=6, max_length=100)

class UserOut(BaseModel):
    id: int
    name: str
    email: EmailStr
    dob: date
    role: UserRole  # <-- Strongly typed enum
    phone_no: Optional[str] = None
    is_verified: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

# ==========================================================
# 2. SellerProfile Schemas
# ==========================================================
class SellerProfileBase(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=50)
    business_desc: Optional[str] = Field(None, max_length=255)


class SellerProfileCreate(SellerProfileBase):
    pass


class SellerProfileUpdate(BaseModel):
    business_name: Optional[str] = Field(None, min_length=1, max_length=50)
    business_desc: Optional[str] = Field(None, max_length=255)


class SellerProfileOut(SellerProfileBase):
    id: int
    user_id: int
    is_verified: bool

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 3. Location Schemas
# ==========================================================
class LocationBase(BaseModel):
    city: str = Field(..., min_length=1, max_length=100)
    country: str = Field(..., min_length=1, max_length=100)
    address_line: str = Field(..., min_length=1, max_length=255)


class LocationCreate(LocationBase):
    seller_id: Optional[int] = None  # Inferred from authenticated seller if omitted


class LocationUpdate(BaseModel):
    city: Optional[str] = Field(None, min_length=1, max_length=100)
    country: Optional[str] = Field(None, min_length=1, max_length=100)
    address_line: Optional[str] = Field(None, min_length=1, max_length=255)


class LocationOut(LocationBase):
    id: int
    seller_id: int

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 4. ServiceTier Schemas
# ==========================================================
class ServiceTierBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    price: Decimal = Field(..., ge=0, decimal_places=2)


class ServiceTierCreate(ServiceTierBase):
    service_id: Optional[int] = None


class ServiceTierUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    price: Optional[Decimal] = Field(None, ge=0, decimal_places=2)


class ServiceTierOut(ServiceTierBase):
    id: int
    service_id: int

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 5. Service Schemas
# ==========================================================
class ServiceBase(BaseModel):
    service_name: str = Field(..., min_length=1, max_length=50)
    service_desc: Optional[str] = Field(None, max_length=255)
    booking_mode: BookingMode = BookingMode.SLOT_CAPACITY
    max_capacity: Optional[int] = Field(None, ge=1)
    base_price: Decimal = Field(..., ge=0, decimal_places=2)


class ServiceCreate(ServiceBase):
    location_id: int
    seller_id: Optional[int] = None


class ServiceUpdate(BaseModel):
    location_id: Optional[int] = None
    service_name: Optional[str] = Field(None, min_length=1, max_length=50)
    service_desc: Optional[str] = Field(None, max_length=255)
    booking_mode: Optional[BookingMode] = None
    max_capacity: Optional[int] = Field(None, ge=1)
    base_price: Optional[Decimal] = Field(None, ge=0, decimal_places=2)


class ServiceOut(ServiceBase):
    id: int
    seller_id: int
    location_id: int
    tiers: List[ServiceTierOut] = []

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 6. InventoryItems Schemas
# ==========================================================
class InventoryItemBase(BaseModel):
    identifier_code: str = Field(..., min_length=1, max_length=20)
    status: ItemStatus = ItemStatus.AVAILABLE


class InventoryItemCreate(InventoryItemBase):
    service_id: int
    tier_id: int


class InventoryItemUpdate(BaseModel):
    identifier_code: Optional[str] = Field(None, min_length=1, max_length=20)
    tier_id: Optional[int] = None
    status: Optional[ItemStatus] = None


class InventoryItemOut(InventoryItemBase):
    id: int
    service_id: int
    tier_id: int

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 7. Booking Schemas
# ==========================================================
class BookingBase(BaseModel):
    service_id: int
    tier_id: Optional[int] = None
    quantity: int = Field(default=1, ge=1)
    start_time: datetime
    end_time: datetime


class BookingCreate(BookingBase):
    assigned_unit_ids: Optional[List[int]] = Field(
        default=None,
        description="Optional unit/inventory item IDs for unit_assigned booking mode",
    )


class BookingStatusUpdate(BaseModel):
    status: BookingStatus


class BookingOut(BookingBase):
    id: int
    user_id: int
    status: BookingStatus
    created_at: datetime
    assigned_units: List[InventoryItemOut] = []

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 8. Payment Schemas
# ==========================================================
class PaymentBase(BaseModel):
    amount: Decimal = Field(..., ge=0, decimal_places=2)
    currency: str = Field(default="usd", min_length=3, max_length=3)


class PaymentCreate(PaymentBase):
    booking_id: int
    idempotency_key: str = Field(..., max_length=255)


class PaymentOut(PaymentBase):
    id: int
    booking_id: int
    stripe_payment_intent_id: str
    client_secret: str
    idempotency_key: str
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 9. Invoice Schemas
# ==========================================================
class InvoiceBase(BaseModel):
    invoice_number: str = Field(..., min_length=1, max_length=100)


class InvoiceCreate(InvoiceBase):
    payment_id: int


class InvoiceOut(InvoiceBase):
    id: int
    payment_id: int
    issued_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 10. Cancellation Schemas
# ==========================================================
class CancellationBase(BaseModel):
    reason: Optional[str] = Field(None, max_length=255)


class CancellationCreate(CancellationBase):
    booking_id: int
    refund_amount: Optional[Decimal] = Field(default=Decimal("0.00"), ge=0, decimal_places=2)


class CancellationOut(CancellationBase):
    id: int
    booking_id: int
    refund_amount: Decimal
    cancelled_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================================
# 11. Review Schemas
# ==========================================================
class ReviewBase(BaseModel):
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 to 5")
    comment: Optional[str] = Field(None, max_length=1000)


class ReviewCreate(ReviewBase):
    booking_id: int


class ReviewUpdate(BaseModel):
    rating: Optional[int] = Field(None, ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=1000)


class ReviewOut(ReviewBase):
    id: int
    booking_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
