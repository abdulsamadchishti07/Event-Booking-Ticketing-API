from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db, model, schema
from app.routers.auth import oauth2

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
    # check seller profile exist or not
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