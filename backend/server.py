"""NestRoute API — Main server orchestrator.

All route handlers are in /routes/*.py.
Shared models in models.py, database config in database.py, helpers in helpers.py.
"""
from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from database import db, client, logger
from helpers import create_notification

# Route modules
from routes.auth import router as auth_router
from routes.students import router as students_router
from routes.drivers import router as drivers_router
from routes.trips import router as trips_router
from routes.notifications import router as notifications_router
from routes.admin import router as admin_router
from routes.billing import router as billing_router
from routes.audit import router as audit_router
from routes.location import router as location_router
from routes.ratings import router as ratings_router

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Create the main app
app = FastAPI(title="NestRoute API")

# Main API router — all sub-routers mount under /api
api_router = APIRouter(prefix="/api")

# Include all route modules
api_router.include_router(auth_router)
api_router.include_router(students_router)
api_router.include_router(drivers_router)
api_router.include_router(trips_router)
api_router.include_router(notifications_router)
api_router.include_router(admin_router)
api_router.include_router(billing_router)
api_router.include_router(audit_router)
api_router.include_router(location_router)
api_router.include_router(ratings_router)


# ======================== HEALTH & ROOT ========================

@api_router.get("/")
async def root():
    return {"message": "NestRoute API", "status": "running"}

@api_router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ======================== BACKGROUND TRIP REMINDERS ========================

async def trip_reminder_loop():
    """Background task: sends reminders to drivers 2hr and 30min before trip start_time"""
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            trips = await db.trips.find({
                "date": today,
                "status": {"$in": ["scheduled", "pending_acceptance"]}
            }, {"_id": 0}).to_list(100)

            for trip in trips:
                start_time = trip.get("start_time", "08:00")
                try:
                    trip_dt = datetime.strptime(f"{trip['date']} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

                diff_minutes = (trip_dt - now).total_seconds() / 60
                trip_id = trip["trip_id"]
                driver_id = trip["driver_id"]

                driver = await db.drivers.find_one({"driver_id": driver_id}, {"_id": 0})
                if not driver:
                    continue
                driver_user_id = driver["user_id"]

                # 2-hour reminder
                if 115 <= diff_minutes <= 125:
                    existing = await db.notifications.find_one({
                        "user_id": driver_user_id,
                        "trip_id": trip_id,
                        "message": {"$regex": "^Reminder: 2 hours"}
                    })
                    if not existing:
                        assignments = await db.trip_assignments.find({"trip_id": trip_id}, {"_id": 0}).to_list(20)
                        await create_notification(
                            driver_user_id,
                            f"Reminder: 2 hours until your trip at {start_time}. {len(assignments)} student(s) to pick up.",
                            "alert", trip_id
                        )
                        logger.info(f"Sent 2hr reminder for trip {trip_id}")

                # 30-minute reminder
                elif 25 <= diff_minutes <= 35:
                    existing = await db.notifications.find_one({
                        "user_id": driver_user_id,
                        "trip_id": trip_id,
                        "message": {"$regex": "^Reminder: 30 minutes"}
                    })
                    if not existing:
                        await create_notification(
                            driver_user_id,
                            f"Reminder: 30 minutes until your trip at {start_time}. Get ready!",
                            "alert", trip_id
                        )
                        logger.info(f"Sent 30min reminder for trip {trip_id}")

        except Exception as e:
            logger.error(f"Trip reminder error: {e}")

        await asyncio.sleep(300)


# ======================== INCLUDE ROUTER & MIDDLEWARE ========================

app.include_router(api_router)

cors_origins = os.environ.get("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=cors_origins.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ======================== STARTUP / SHUTDOWN ========================

@app.on_event("startup")
async def create_indexes():
    await db.users.create_index("user_id", unique=True)
    await db.users.create_index("email", unique=True)
    await db.students.create_index("student_id", unique=True)
    await db.students.create_index("parent_id")
    await db.drivers.create_index("driver_id", unique=True)
    await db.drivers.create_index("user_id", unique=True)
    await db.trips.create_index("trip_id", unique=True)
    await db.trips.create_index("driver_id")
    await db.trips.create_index("date")
    await db.trips.create_index([("date", 1), ("status", 1)])
    await db.trip_assignments.create_index("assignment_id", unique=True)
    await db.trip_assignments.create_index("trip_id")
    await db.trip_assignments.create_index("student_id")
    await db.notifications.create_index("notification_id", unique=True)
    await db.notifications.create_index([("user_id", 1), ("created_at", -1)])
    await db.user_sessions.create_index("session_token", unique=True)
    await db.user_sessions.create_index("user_id")
    await db.push_tokens.create_index("user_id", unique=True)
    await db.driver_locations.create_index("trip_id", unique=True)
    await db.audit_logs.create_index("log_id", unique=True)
    await db.audit_logs.create_index([("trip_id", 1), ("timestamp", 1)])
    await db.sms_logs.create_index("sms_id", unique=True)
    await db.sms_logs.create_index([("trip_id", 1), ("event_type", 1), ("phone", 1), ("sent_at", -1)])
    await db.subscriptions.create_index("user_id", unique=True)
    await db.subscriptions.create_index("stripe_subscription_id")
    await db.subscriptions.create_index("org_id")
    await db.organizations.create_index("org_id", unique=True)
    await db.ratings.create_index("rating_id", unique=True)
    await db.ratings.create_index([("parent_id", 1), ("driver_id", 1)], unique=True)
    await db.ratings.create_index("driver_id")
    logger.info("MongoDB indexes created")
    asyncio.create_task(trip_reminder_loop())
    logger.info("Trip reminder background task started")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
