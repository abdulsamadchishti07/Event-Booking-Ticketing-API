from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import oauth2
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
    db: Session= Depends(get_db)
):
    # seller has a active  profile
    if not current_user.seller_profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have a active profile"
        )

    # Check if venue/location exists
    location = db.query(model.Location).filter(model.Location.id==services_in.location_id).first()
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location with id {services_in.location_id} not found"
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

    # Create the service linked to current seller
    new_services = model.Services(
        seller_id=current_user.seller_profile.id,
        location_id=services_in.location_id,
        service_name=services_in.service_name,
        service_desc=services_in.service_desc,
        booking_mode=services_in.booking_mode,
        max_capacity=services_in.max_capacity,
        base_price=services_in.base_price
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
    db: Session = Depends(get_db)
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
    
    # Check for duplicate tier name on this service
    existing_tier = db.query(model.ServiceTier).filter(
        model.ServiceTier.service_id == service_id,
        model.ServiceTier.name.ilike(tier_in.name)
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
    db: Session = Depends(get_db)
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
        for code in inventory_in.identifier_codes
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

    return {
        "message": f"Successfully created {len(new_items)} inventory items",
        "created_count": len(new_items)
    }