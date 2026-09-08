from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def main():
    return {"message": "Hello from event-booking-ticketing-api!"}


if __name__ == "__main__":
    main()


