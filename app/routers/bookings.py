from datetime import datetime, timedelta, timezone
from typing import Annotated, List

from fastapi import APIRouter, BackgroundTasks, Depends, status, HTTPException
import redis.asyncio as aioredis
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_

from app.core import oauth2, redis as redis_client
from app.core.cache import invalidate_services_cache
from app.core.config import settings
from app.core.redis import get_redis
from app.database import get_db, model, schema


router = APIRouter(
    prefix="/bookings",
    tags=["Bookings & Reservations"]
)

HOLD_DURATION_MINUTES = settings.HOLD_DURATION_MINUTES
MAX_TICKETS_PER_BOOKING = 10
MAX_PENDING_HOLDS_PER_USER = 5


# ==========================================================
# 1. Create 10-Minute Hold Booking
# ==========================================================
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=schema.BookingOut,
    summary="Create a 10-minute temporary reservation hold",
    dependencies=[
        Depends(
            redis_client.IPRateLimiting(
                max_requests=60,
                window_seconds=60
            )
        )
    ]
)
def create_booking(
    booking_in: schema.BookingCreate,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    now = datetime.now(timezone.utc)

    # 0. Anti-Hoarding Protections: Cap tickets per booking & active pending holds per user
    requested_qty = len(booking_in.assigned_unit_ids) if booking_in.assigned_unit_ids else booking_in.quantity
    if requested_qty > MAX_TICKETS_PER_BOOKING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot book more than {MAX_TICKETS_PER_BOOKING} tickets per booking"
        )

    active_user_holds = db.query(model.Booking.id).filter(
        model.Booking.user_id == current_user.id,
        model.Booking.status == model.BookingStatus.PENDING,
        model.Booking.created_at >= now - timedelta(minutes=HOLD_DURATION_MINUTES)
    ).count()
    if active_user_holds >= MAX_PENDING_HOLDS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"You have reached the maximum limit of {MAX_PENDING_HOLDS_PER_USER} active pending reservation holds. Please complete or cancel existing pending bookings."
        )

    # 1. Fetch the service
    service = db.query(model.Services).filter(model.Services.id == booking_in.service_id).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {booking_in.service_id} not found"
        )

    # Validate event has not already started
    service_start = service.start_time
    if service_start.tzinfo is None:
        service_start = service_start.replace(tzinfo=timezone.utc)
    if now >= service_start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot book tickets for an event that has already started"
        )

    # Validate tier belongs to this service if specified
    if booking_in.tier_id:
        tier = db.query(model.ServiceTier).filter(
            model.ServiceTier.id == booking_in.tier_id,
            model.ServiceTier.service_id == service.id
        ).first()
        if not tier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected tier does not belong to this service"
            )
    
    # 2. Case A: unit_assigned (Reserved Seating with Pessimistic Locking)
    if service.booking_mode == model.BookingMode.UNIT_ASSIGNED:
        if not booking_in.assigned_unit_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="assigned_unit_ids are required for unit_assigned booking mode"
            )
        if len(booking_in.assigned_unit_ids) != len(set(booking_in.assigned_unit_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate seat IDs are not allowed in assigned_unit_ids"
            )
        
        # PESSIMISTIC LOCK: Lock the exact requested seat rows in PostgreSQL ordered deterministically to prevent deadlocks
        seats = db.query(model.InventoryItems).filter(
            model.InventoryItems.id.in_(booking_in.assigned_unit_ids),
            model.InventoryItems.service_id == service.id
        ).order_by(model.InventoryItems.id.asc()).with_for_update().all()

        # Check if all requested seats exist
        if len(seats) != len(booking_in.assigned_unit_ids):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or more selected seats were not found for this service"
            )

        # Check if seats match tier if tier_id was explicitly requested
        if booking_in.tier_id:
            for seat in seats:
                if seat.tier_id != booking_in.tier_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Seat '{seat.identifier_code}' does not belong to the selected tier"
                    )

        # Check if any seat is already taken
        for seat in seats:
            if seat.status != model.ItemStatus.AVAILABLE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Seat '{seat.identifier_code}' is currently unavailable (status: {seat.status})"
                )
        
        # Change seat status to RESERVED for the 10-minute hold
        for seat in seats:
            seat.status = model.ItemStatus.RESERVED
        
        # Create the booking record
        new_booking = model.Booking(
            user_id=current_user.id,
            service_id=service.id,
            tier_id=booking_in.tier_id,
            quantity=len(seats),
            start_time=service.start_time,
            end_time=service.end_time,
            status=model.BookingStatus.PENDING,
        )
        new_booking.assigned_units = seats
        
        db.add(new_booking)
        db.commit()
        db.refresh(new_booking)
        
        background_tasks.add_task(invalidate_services_cache, service_id=service.id, redis=redis)
        return new_booking

    # 3. Case B: slot_capacity (General Admission with Atomic Capacity Lock)
    elif service.booking_mode == model.BookingMode.SLOT_CAPACITY:
        if booking_in.assigned_unit_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="assigned_unit_ids cannot be provided for slot_capacity booking mode"
            )

        # PESSIMISTIC LOCK: Lock the service row while checking capacity
        lock_service = db.query(model.Services).filter(model.Services.id == service.id).with_for_update().first()

        # Count active tickets (confirmed + active non-expired pending holds)
        active_booking_count = db.query(func.coalesce(func.sum(model.Booking.quantity), 0)).filter(
            model.Booking.service_id == service.id,
            or_(
                model.Booking.status == model.BookingStatus.CONFIRMED,
                and_(
                    model.Booking.status == model.BookingStatus.PENDING,
                    model.Booking.created_at >= now - timedelta(minutes=HOLD_DURATION_MINUTES)
                )
            )
        ).scalar()

        if lock_service.max_capacity is None or (active_booking_count + booking_in.quantity) > lock_service.max_capacity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Sold out! Only {max(0, (lock_service.max_capacity or 0) - active_booking_count)} tickets remaining"
            )

        new_booking = model.Booking(
            user_id=current_user.id,
            service_id=service.id,
            tier_id=booking_in.tier_id,
            quantity=booking_in.quantity,
            start_time=service.start_time,
            end_time=service.end_time,
            status=model.BookingStatus.PENDING,
        )

        db.add(new_booking)
        db.commit()
        db.refresh(new_booking)

        background_tasks.add_task(invalidate_services_cache, service_id=service.id, redis=redis)
        return new_booking


# ==========================================================
# 2. Get User's Bookings (/bookings/me)
# ==========================================================
@router.get(
    "/me",
    response_model=List[schema.BookingOut],
    summary="Get current customer's bookings"
)
def get_my_bookings(
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    """
    Returns list of all bookings created by the authenticated customer.
    """
    return db.query(model.Booking).filter(
        model.Booking.user_id == current_user.id
    ).order_by(model.Booking.created_at.desc()).all()


# ==========================================================
# 3. Get Booking Details by ID (/bookings/{booking_id})
# ==========================================================
@router.get(
    "/{booking_id}",
    response_model=schema.BookingOut,
    summary="Get booking details by ID"
)
def get_booking_by_id(
    booking_id: int,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    """
    Fetches booking details by ID. Restricted to the booking owner or Admin.
    """
    booking = db.query(model.Booking).filter(model.Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking with id {booking_id} not found"
        )

    if booking.user_id != current_user.id and current_user.role != model.UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to view this booking"
        )

    return booking