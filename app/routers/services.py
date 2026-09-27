from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
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

    db.add(new_services)
    db.commit()
    db.refresh(new_services)

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
            detail=f"Service with id {service_id} not found"
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

    db.add(new_tier)
    db.commit()
    db.refresh(new_tier)

    return new_tier