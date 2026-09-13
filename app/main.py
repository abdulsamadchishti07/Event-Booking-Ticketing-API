from fastapi import FastAPI

from .routers import users

app = FastAPI(
    title="Event Booking & Ticketing API",
    description="Production-ready Event Booking and Ticketing REST API.",
    version="1.0.0"
)

# Include API routers
app.include_router(users.router)


@app.get("/", tags=["Health"])
def root():
    """Health check endpoint to verify API availability."""
    return {"status": "healthy", "message": "Event Booking & Ticketing API is running!"}
