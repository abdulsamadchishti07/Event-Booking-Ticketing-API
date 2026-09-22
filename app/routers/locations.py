from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db, model, schema
from app.core import oauth2

router = APIRouter(
    prefix="/locations",
    tags=["Locations"]
)

@router.post("", 
    status_code=status.HTTP_201_CREATED,
    response_model=schema.LocationOut,
    summary="Create a new location"
)
def create_location(
    location: schema.LocationCreate,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    db: Session= Depends(get_db)
):
    # check seller profile exist or nots
    if not current_user.seller_profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have seller profile"
        )
    
    # create the location linked to seller profile
    new_location = model.Location(
        seller_id=current_user.seller_profile.id,
        city=location.city,
        country=location.country,
        address_line=location.address_line
    )

    db.add(new_location)
    db.commit()
    db.refresh(new_location)

    return new_location

@router.get(
    "",
    response_model=list[schema.LocationOut],
    summary="Get all locations"
)
def get_locations(
    db: Session = Depends(get_db)
):
    locations = db.query(model.Location).all()
    return locations


@router.get(

    "/{id}",
    response_model=schema.LocationOut,
    summary="Get location by ID"
)
def get_location_by_id(
    id: int,
    db: Session = Depends(get_db)
):
    location = db.query(model.Location).filter(model.Location.id == id).first()
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location with id {id} not found"
        )
    return location


@router.put(
    "/{id}",
    response_model=schema.LocationOut,
    summary="Update a location"
)
def update_location(
    id: int,
    location_in: schema.LocationUpdate,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    db: Session = Depends(get_db)
):
    location = db.query(model.Location).filter(model.Location.id == id).first()
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location with id {id} not found"
        )

    # Ownership check
    if location.seller_id != current_user.seller_profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to update this location"
        )

    # Update only provided fields
    update_data = location_in.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(location, key, value)

    db.commit()
    db.refresh(location)
    return location


@router.delete(
    "/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a location"
)
def delete_location(
    id: int,
    current_user: Annotated[model.User, Depends(oauth2.require_seller)],
    db: Session = Depends(get_db)
):
    location = db.query(model.Location).filter(model.Location.id == id).first()
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location with id {id} not found"
        )

    # Ownership check
    if location.seller_id != current_user.seller_profile.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this location"
        )

    db.delete(location)
    db.commit()
    return None
