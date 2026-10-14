from datetime import datetime, timedelta, timezone
import json
from typing import Annotated, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
import redis.asyncio as aioredis
from sqlalchemy import and_, asc, desc, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.concurrency import run_in_threadpool

from app.core import oauth2
from app.core.cache import (
    get_cached,
    set_cached,
    make_services_list_cache_key,
    make_service_detail_cache_key,
    make_service_seats_cache_key,
    invalidate_services_cache,
)
from app.core.config import settings
from app.core.redis import get_redis, IPRateLimiting
from app.database import get_db, model, schema


router = APIRouter(
    prefix ="/services",
    tags=["Services & Events"]
)



# ==========================================================
# 1. Create Services
# ==========================================================
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=schema.ServiceOut,
    summary="Create a new event/booking"
)
def create_services(
    services_in: schema.ServiceCreate,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    # seller has a active  profile
    if not current_user.seller_profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have a active profile"
        )

    # Validate event start time is in the future (normalize to UTC)
    now = datetime.now(timezone.utc)
    start_time = services_in.start_time.astimezone(timezone.utc)
    end_time = services_in.end_time.astimezone(timezone.utc)
    if start_time < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create an event with a start_time in the past"
        )

    # Check if venue/location exists
    location = db.query(model.Location).filter(model.Location.id==services_in.location_id).first()
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location with id {services_in.location_id} not found"
        )

    # Ownership check: seller must own this venue location
    if location.seller_id != current_user.seller_profile.id and current_user.role != model.UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to host services at this location"
        )
    
    # Validate slot_capacity requires max_capacity
    if services_in.booking_mode == model.BookingMode.SLOT_CAPACITY and not services_in.max_capacity:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_capacity is required for slot_capacity booking mode"
        )

    # Validate unit_assigned cannot have max_capacity
    if services_in.booking_mode == model.BookingMode.UNIT_ASSIGNED and services_in.max_capacity is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_capacity cannot be set for unit_assigned booking mode"
        )

    # Create the service linked to current seller with normalized UTC datetimes
    new_services = model.Services(
        seller_id=current_user.seller_profile.id,
        location_id=services_in.location_id,
        service_name=services_in.service_name,
        service_desc=services_in.service_desc,
        booking_mode=services_in.booking_mode,
        max_capacity=services_in.max_capacity,
        base_price=services_in.base_price,
        start_time=start_time,
        end_time=end_time,
    )

    try:
        db.add(new_services)
        db.commit()
        db.refresh(new_services)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not create service due to database constraint"
        )

    background_tasks.add_task(invalidate_services_cache, redis=redis)
    return new_services

# ==========================================================
# 2. Add Pricing Tier to an Event
# ==========================================================
@router.post(

    "/{service_id}/tiers",
    status_code=status.HTTP_201_CREATED,
    response_model=schema.ServiceTierOut,
    summary="Pricing tier to an event"
)
def create_service_tier(
    service_id: int,
    tier_in: schema.ServiceTierCreate,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    # Verifyied Seller
    if not current_user.seller_profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have an active seller profile"
        )
    
    # Check if service exits or not
    serivice = db.query(model.Services).filter(model.Services.id == service_id).first()
    if not serivice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found"
            )
    
    # ownership check only owner can make the ticket
    if serivice.seller_id != current_user.seller_profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to modify this service"
        )
    
    # Check for duplicate tier name on this service (case-insensitive exact comparison)
    existing_tier = db.query(model.ServiceTier).filter(
        model.ServiceTier.service_id == service_id,
        func.lower(model.ServiceTier.name) == tier_in.name.strip().lower()
    ).first()
    if existing_tier:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tier with name '{tier_in.name}' already exists for this service"
        )

    new_tier = model.ServiceTier(
        service_id=service_id,
        name=tier_in.name,
        price=tier_in.price
    )

    try:
        db.add(new_tier)
        db.commit()
        db.refresh(new_tier)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tier with name '{tier_in.name}' already exists for this service"
        )

    background_tasks.add_task(invalidate_services_cache, service_id=service_id, redis=redis)
    return new_tier

# ==========================================================
# 3. Bulk Generate Inventory Items (Seats/Units)
# ==========================================================
@router.post(
    "/{service_id}/inventory/bulk",
    status_code=status.HTTP_201_CREATED,
    summary="Bulk generate seats/units for an event"
)
def bulk_create_inventory(
    service_id: int,
    inventory_in: schema.BulkInventoryCreate,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):

    # 1
    if not current_user.seller_profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have an active seller profile"
        )

    # 2
    service = db.query(model.Services).filter(model.Services.id == service_id).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found"
        )
    
    # 3
    # ownership check only owner can make the ticket
    if service.seller_id != current_user.seller_profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to modify this service"
        )
    
    # 4
    # only unit_assigind can hace seats of the units generates
    if service.booking_mode != model.BookingMode.UNIT_ASSIGNED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bulk inventory generation is only allowed for unit_assigned services"
        )

    # 5
    # check it already exists and belong to the same service
    tier = db.query(model.ServiceTier).filter(
        model.ServiceTier.id == inventory_in.tier_id,
        model.ServiceTier.service_id == service_id
    ).first()
    if not tier:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tier with id {inventory_in.tier_id} not found for this service"
        )

    # 6 
    # Check for duplicate identifier codes in database for this service
    existing_items = db.query(model.InventoryItems).filter(
        model.InventoryItems.service_id == service_id,
        model.InventoryItems.identifier_code.in_(inventory_in.identifier_codes)
    ).all()
    if existing_items:
        duplicates = [item.identifier_code for item in existing_items]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Identifier codes already exist: {duplicates}"
        )

    # 7
    # Bulk create all seats
    new_items = [
        model.InventoryItems(
            service_id=service_id,
            tier_id=inventory_in.tier_id,
            identifier_code=code,
            status=model.ItemStatus.AVAILABLE
        )
        for code in inventory_in.identifier_codes   # Well it will take O(n) time to make the seats
    ]

    try:
        db.add_all(new_items)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="One or more identifier codes already exist for this service"
        )

    background_tasks.add_task(invalidate_services_cache, service_id=service_id, redis=redis)
    return {
        "message": f"Successfully created {len(new_items)} inventory items",
        "created_count": len(new_items)
    }
# ==========================================================
# 4. Public Discovery: Search and Filter Events
# ==========================================================
# 4. Public Discovery: Search & Filter Events
# ==========================================================
@router.get(
    "",
    status_code=status.HTTP_200_OK,
    response_model=List[schema.ServiceOut],
    summary="Public discovery of events",
    dependencies=[
        Depends(
            IPRateLimiting(
                max_requests=settings.services_rate_limit,
                window_seconds=60
            )
        )
    ]
)
async def search_services(
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    city: Optional[str] = None,
    country: Optional[str] = None,
    booking_mode: Optional[model.BookingMode] = None,
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    date_from: Optional[datetime] = Query(None, description="Filter events starting on or after this UTC timestamp"),
    date_to: Optional[datetime] = Query(None, description="Filter events starting on or before this UTC timestamp"),
    include_past: bool = Query(default=False, description="Whether to include past events (default False)"),
    sort_by: Optional[str] = Query(
        None,
        pattern="^(price_asc|price_desc|date_asc|date_desc)$",
        description="Sort by: price_asc, price_desc, date_asc, or date_desc"
    ),
    limit: int = Query(default=100, ge=1, le=100, description="Items per page (max 100)"),
    offset: int = Query(default=0, ge=0, description="Pagination offset")
):
    if min_price is not None and max_price is not None:
        if min_price > max_price:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="min_price cannot be greater than max_price"
            )

    if date_from is not None and date_to is not None:
        if date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="date_from cannot be greater than date_to"
            )

    cache_key = make_services_list_cache_key(
        city=city,
        country=country,
        booking_mode=booking_mode,
        min_price=min_price,
        max_price=max_price,
        date_from=date_from,
        date_to=date_to,
        include_past=include_past,
        sort_by=sort_by,
        limit=limit,
        offset=offset
    )

    cached_data = await get_cached(cache_key, redis=redis)
    if cached_data is not None:
        return Response(
            content=cached_data,
            media_type="application/json",
            headers={"X-Cache": "HIT"}
        )

    query = db.query(model.Services).options(
        joinedload(model.Services.location),
        joinedload(model.Services.seller),
        selectinload(model.Services.tiers)
    ).join(model.Location)

    if city:
        query = query.filter(model.Location.city.ilike(f"%{city}%"))
    if country:
        query = query.filter(model.Location.country.ilike(f"%{country}%"))
    if booking_mode:
        query = query.filter(model.Services.booking_mode == booking_mode)
    if min_price is not None:
        query = query.filter(model.Services.base_price >= min_price)
    if max_price is not None:
        query = query.filter(model.Services.base_price <= max_price)

    # Date Filtering: Default to upcoming events only (start_time >= now()) unless include_past=True
    now = datetime.now(timezone.utc)
    if date_from:
        query = query.filter(model.Services.start_time >= date_from)
    elif not include_past:
        query = query.filter(model.Services.start_time >= now)

    if date_to:
        query = query.filter(model.Services.start_time <= date_to)

    # Sorting
    if sort_by == "date_asc":
        query = query.order_by(asc(model.Services.start_time), asc(model.Services.id))
    elif sort_by == "date_desc":
        query = query.order_by(desc(model.Services.start_time), desc(model.Services.id))
    elif sort_by == "price_asc":
        query = query.order_by(asc(model.Services.base_price), asc(model.Services.id))
    elif sort_by == "price_desc":
        query = query.order_by(desc(model.Services.base_price), desc(model.Services.id))
    else:
        query = query.order_by(asc(model.Services.start_time), asc(model.Services.id))

    services = await run_in_threadpool(lambda: query.offset(offset).limit(limit).all())

    serialized = [
        schema.ServiceOut.model_validate(s).model_dump(mode="json")
        for s in services
    ]
    json_data = json.dumps(serialized)

    await set_cached(cache_key, json_data, ttl=settings.cache_services_ttl, redis=redis)

    return Response(
        content=json_data,
        media_type="application/json",
        headers={"X-Cache": "MISS"}
    )


# ==========================================================
# 5. Public Discovery: Live Seat Map
# ==========================================================
@router.get(
    "/{service_id}/seats",
    status_code=status.HTTP_200_OK,
    response_model=List[schema.InventoryItemOut],
    summary="Get live seat map for an event",
    dependencies=[
        Depends(
            IPRateLimiting(
                max_requests=settings.services_rate_limit,
                window_seconds=60
            )
        )
    ]
)
async def get_service_seats(
    service_id: int,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    cache_key = make_service_seats_cache_key(service_id)
    cached_data = await get_cached(cache_key, redis=redis)
    if cached_data is not None:
        return Response(
            content=cached_data,
            media_type="application/json",
            headers={"X-Cache": "HIT"}
        )

    service = await run_in_threadpool(
        lambda: db.query(model.Services).filter(model.Services.id == service_id).first()
    )
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found"
        )
    
    if service.booking_mode != model.BookingMode.UNIT_ASSIGNED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Seat maps are only available for unit_assigned events"
        )

    seats = await run_in_threadpool(
        lambda: (
            db.query(model.InventoryItems)
            .options(joinedload(model.InventoryItems.tier))
            .filter(model.InventoryItems.service_id == service_id)
            .order_by(model.InventoryItems.id.asc())
            .all()
        )
    )

    serialized = [
        schema.InventoryItemOut.model_validate(s).model_dump(mode="json")
        for s in seats
    ]
    json_data = json.dumps(serialized)

    await set_cached(cache_key, json_data, ttl=settings.cache_seats_ttl, redis=redis)

    return Response(
        content=json_data,
        media_type="application/json",
        headers={"X-Cache": "MISS"}
    )


@router.get(
    "/{service_id}",
    response_model=schema.ServiceOut,
    summary="Get service/event details by ID"
)
async def get_service_by_id(
    service_id: int,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    cache_key = make_service_detail_cache_key(service_id)
    cached_data = await get_cached(cache_key, redis=redis)
    if cached_data is not None:
        return Response(
            content=cached_data,
            media_type="application/json",
            headers={"X-Cache": "HIT"}
        )

    service = await run_in_threadpool(
        lambda: (
            db.query(model.Services)
            .options(
                joinedload(model.Services.location),
                joinedload(model.Services.seller),
                selectinload(model.Services.tiers)
            )
            .filter(model.Services.id == service_id)
            .first()
        )
    )
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found"
        )

    serialized = schema.ServiceOut.model_validate(service).model_dump(mode="json")
    json_data = json.dumps(serialized)

    await set_cached(cache_key, json_data, ttl=settings.cache_services_ttl, redis=redis)

    return Response(
        content=json_data,
        media_type="application/json",
        headers={"X-Cache": "MISS"}
    )


# ==========================================================
# 6. Update Service (Seller Only)
# ==========================================================
@router.put(
    "/{service_id}",
    response_model=schema.ServiceOut,
    summary="Update an existing service/event"
)
def update_service(
    service_id: int,
    service_update: schema.ServiceUpdate,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    service = db.query(model.Services).filter(model.Services.id == service_id).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found"
        )
    seller_id = current_user.seller_profile.id if current_user.seller_profile else None
    if current_user.role != model.UserRole.ADMIN and service.seller_id != seller_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to modify this service"
        )

    update_data = service_update.model_dump(exclude_unset=True)

    # 1. Location validation: prevent hijacking other seller's venue
    if "location_id" in update_data and update_data["location_id"] is not None:
        target_loc = db.query(model.Location).filter(model.Location.id == update_data["location_id"]).first()
        if not target_loc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Location with id {update_data['location_id']} not found"
            )
        if current_user.role != model.UserRole.ADMIN and target_loc.seller_id != seller_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot assign a location owned by another seller"
            )

    # 2. Booking mode immutability: cannot change booking_mode if event has bookings or inventory
    if "booking_mode" in update_data and update_data["booking_mode"] is not None:
        if update_data["booking_mode"] != service.booking_mode:
            has_bookings = db.query(model.Booking.id).filter(model.Booking.service_id == service.id).first()
            if has_bookings:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot change booking_mode on an event with existing bookings"
                )
            has_inventory = db.query(model.InventoryItems.id).filter(model.InventoryItems.service_id == service.id).first()
            if has_inventory:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot change booking_mode on an event with existing inventory"
                )
            if update_data["booking_mode"] == model.BookingMode.SLOT_CAPACITY:
                new_cap = update_data.get("max_capacity", service.max_capacity)
                if new_cap is None or new_cap < 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="max_capacity is required and must be >= 1 when switching to slot_capacity mode"
                    )
            elif update_data["booking_mode"] == model.BookingMode.UNIT_ASSIGNED:
                update_data["max_capacity"] = None

    # 3. Capacity validation: prevent setting null or shrinking below active bookings
    effective_mode = update_data.get("booking_mode", service.booking_mode)
    if "max_capacity" in update_data:
        new_cap = update_data["max_capacity"]
        if effective_mode == model.BookingMode.SLOT_CAPACITY:
            if new_cap is None or new_cap < 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="max_capacity cannot be null or less than 1 for slot_capacity mode"
                )
            now = datetime.now(timezone.utc)
            active_count = db.query(func.coalesce(func.sum(model.Booking.quantity), 0)).filter(
                model.Booking.service_id == service.id,
                or_(
                    model.Booking.status == model.BookingStatus.CONFIRMED,
                    and_(
                        model.Booking.status == model.BookingStatus.PENDING,
                        model.Booking.created_at >= now - timedelta(minutes=settings.HOLD_DURATION_MINUTES)
                    )
                )
            ).scalar()
            if new_cap < active_count:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot reduce capacity to {new_cap}; {active_count} tickets are already booked or held"
                )

    start_time = update_data.get("start_time", service.start_time)
    end_time = update_data.get("end_time", service.end_time)

    # Normalize both times to UTC to avoid comparing offset-naive and offset-aware datetimes
    if start_time is not None:
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        else:
            start_time = start_time.astimezone(timezone.utc)
        if "start_time" in update_data:
            update_data["start_time"] = start_time

    if end_time is not None:
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        else:
            end_time = end_time.astimezone(timezone.utc)
        if "end_time" in update_data:
            update_data["end_time"] = end_time

    if "start_time" in update_data and update_data["start_time"] is not None:
        now = datetime.now(timezone.utc)
        if start_time < now:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot update an event to have a start_time in the past"
            )

    if start_time is not None and end_time is not None and end_time <= start_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end_time must be strictly after start_time"
        )

    for key, value in update_data.items():
        setattr(service, key, value)
    db.commit()
    db.refresh(service)

    background_tasks.add_task(invalidate_services_cache, service_id=service_id, redis=redis)
    return service


# ==========================================================
# 7. Delete Service (Seller Only)
# ==========================================================
@router.delete(
    "/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a service/event"
)
def delete_service(
    service_id: int,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis)
):
    service = db.query(model.Services).filter(model.Services.id == service_id).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found"
        )
    seller_id = current_user.seller_profile.id if current_user.seller_profile else None
    if current_user.role != model.UserRole.ADMIN and service.seller_id != seller_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this service"
        )

    # Prevent deleting an event that has any existing bookings (pending or confirmed)
    has_bookings = db.query(model.Booking).filter(
        model.Booking.service_id == service_id
    ).first()
    if has_bookings:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete event with existing {has_bookings.status.value} bookings"
        )

    db.delete(service)
    db.commit()

    background_tasks.add_task(invalidate_services_cache, service_id=service_id, redis=redis)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

