from datetime import datetime, timezone
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core import oauth2
from app.database import get_db, model, schema


router = APIRouter(
    tags=["Reviews & Ratings"]
)


# ==========================================================
# 1. CREATE REVIEW
# ==========================================================
@router.post(
    "/reviews",
    response_model=schema.ReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a review for an attended confirmed booking"
)
def create_review(
    review_in: schema.ReviewCreate,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    now = datetime.now(timezone.utc)
    # 1. Fetch booking
    booking = db.query(model.Booking).filter(model.Booking.id == review_in.booking_id).first()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking with id {review_in.booking_id} not found"
        )
    # 2. Ownership verification
    if booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to review this booking"
        )
    # 3. Booking status validation
    if booking.status != model.BookingStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only confirmed paid bookings can be reviewed (current status: {booking.status})"
        )
    # 4. Event completion check (event must have finished)
    if booking.service:
        event_end = booking.service.end_time or booking.service.start_time
        if event_end:
            if event_end.tzinfo is None:
                event_end = event_end.replace(tzinfo=timezone.utc)
            if now < event_end:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot review an event before it has taken place"
                )
    # 5. Check if review already exists for this booking
    existing_review = db.query(model.Review).filter(model.Review.booking_id == booking.id).first()
    if existing_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already reviewed this booking. Use PUT /reviews/{id} to update your review."
        )
    # 6. Create review
    review = model.Review(
        booking_id=booking.id,
        rating=review_in.rating,
        comment=review_in.comment
    )
    db.add(review)
    try:
        db.commit()
        db.refresh(review)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already reviewed this booking."
        )
    return review

# ==========================================================
# 2. GET EVENT REVIEWS
# ==========================================================
@router.get(
    "/services/{service_id}/reviews",
    response_model=schema.ServiceReviewsSummary,
    status_code=status.HTTP_200_OK,
    summary="Get all public reviews and average rating for an event"
)
def get_service_reviews(
    service_id: int,
    db: Session = Depends(get_db)
):
    service = db.query(model.Services).filter(model.Services.id == service_id).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found"
        )
    reviews = (
        db.query(model.Review)
        .join(model.Booking)
        .filter(model.Booking.service_id == service_id)
        .order_by(model.Review.created_at.desc())
        .all()
    )
    total_reviews = len(reviews)
    avg_rating = round(sum(r.rating for r in reviews) / total_reviews, 1) if total_reviews > 0 else 0.0
    return schema.ServiceReviewsSummary(
        service_id=service_id,
        average_rating=avg_rating,
        total_reviews=total_reviews,
        reviews=reviews
    )
# ==========================================================
# 3. UPDATE REVIEW 
# ==========================================================
@router.put(
    "/reviews/{review_id}",
    response_model=schema.ReviewOut,
    status_code=status.HTTP_200_OK,
    summary="Update an existing review"
)
def update_review(
    review_id: int,
    review_update: schema.ReviewUpdate,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    review = db.query(model.Review).filter(model.Review.id == review_id).first()
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review with id {review_id} not found"
        )
    # Ownership check
    if review.booking.user_id != current_user.id and current_user.role != model.UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to edit this review"
        )
    update_data = review_update.model_dump(exclude_unset=True)
    if "rating" in update_data:
        review.rating = update_data["rating"]
    if "comment" in update_data:
        review.comment = update_data["comment"]
    db.commit()
    db.refresh(review)
    return review
# ==========================================================
# 4. DELETE REVIEW
# ==========================================================
@router.delete(
    "/reviews/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a review"
)
def delete_review(
    review_id: int,
    current_user: Annotated[model.User, Depends(oauth2.get_verified_user)],
    db: Session = Depends(get_db)
):
    review = db.query(model.Review).filter(model.Review.id == review_id).first()
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review with id {review_id} not found"
        )
    if review.booking.user_id != current_user.id and current_user.role != model.UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to delete this review"
        )
    db.delete(review)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)