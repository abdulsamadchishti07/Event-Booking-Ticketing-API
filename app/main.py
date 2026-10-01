import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI

from .routers import locations, services, bookings
from .routers.auth import auth, users
from .core.tasks import run_hold_sweeper_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the background sweeper when the server boots
    sweeper_task = asyncio.create_task(run_hold_sweeper_loop())
    yield
    # Cleanly cancel it when the server shuts down
    sweeper_task.cancel()



app = FastAPI(
    title="Event Booking & Ticketing API",
    description="Production-ready Event Booking and Ticketing REST API.",
    version="1.0.0",
    lifespan=lifespan
)

# Include API routers
app.include_router(users.router)
app.include_router(auth.router)
app.include_router(locations.router)
app.include_router(services.router)
app.include_router(bookings.router)

@app.get("/", tags=["Health"])
def root():
    """Health check endpoint to verify API availability."""
    return {"status": "healthy", "message": "Event Booking & Ticketing API is running!"}
