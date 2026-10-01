import asyncio
from datetime import datetime, timedelta, timezone
from app.database import SessionLocal, model

from sqlalchemy.orm import Session

def released_expired_holds(db: Session = None):
    """
    Finds PENDING bookings older than 10 minutes, cancels them,
    and returns any assigned seats to AVAILABLE status.
    """

    close_session = (db is None)
    db = db or SessionLocal()

    try:
        now = datetime.now(timezone.utc)
        # Anything created more than 10 minutes ago is expired
        
        expire_thresold = now - timedelta(minutes=10)

        expire_booking = db.query(model.Booking).filter(
            model.Booking.status == model.BookingStatus.PENDING,
            model.Booking.created_at <= expire_thresold
        ).all()

        for booking in expire_booking:
            booking.status = model.BookingStatus.CANCELLED

            for seats in booking.assigned_units:
                seats.status = model.ItemStatus.AVAILABLE
            
        if expire_booking:
            db.commit()
        
    finally:
        if close_session:
            db.close()
        
async def run_hold_sweeper_loop():
    """Infinite background loop running every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        # We run the synchronous DB code in a separate thread 
        # so we don't freeze the FastAPI asyncio event loop!
        await asyncio.to_thread(released_expired_holds)