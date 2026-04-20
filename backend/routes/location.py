from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *

router = APIRouter()

@router.post("/location/update")
async def update_driver_location(location: LocationUpdate, request: Request):
    """Driver sends GPS location — stored as latest only (no history)"""
    user = await get_driver_user(request)
    
    # Verify trip exists and is in_progress
    trip = await db.trips.find_one({"trip_id": location.trip_id}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    if trip["status"] != "in_progress":
        raise HTTPException(status_code=400, detail="Trip is not active")
    
    # Get driver profile
    driver = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    now = datetime.now(timezone.utc)
    
    # Upsert — only store latest location per trip (no history)
    await db.driver_locations.update_one(
        {"trip_id": location.trip_id},
        {
            "$set": {
                "driver_id": driver["driver_id"],
                "trip_id": location.trip_id,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "timestamp": now,
                "user_id": user.user_id
            }
        },
        upsert=True
    )
    
    # Broadcast location to parents via WebSocket
    assignments = await db.trip_assignments.find({"trip_id": location.trip_id}, {"_id": 0}).to_list(100)
    student_ids = [a["student_id"] for a in assignments]
    students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(100)
    parent_ids = list(set(s["parent_id"] for s in students))
    
    await manager.broadcast_to_users({
        "type": "driver_location",
        "data": {
            "trip_id": location.trip_id,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timestamp": now.isoformat(),
            "driver_name": user.name
        }
    }, parent_ids)
    
    return {"message": "Location updated"}

@router.get("/location/{trip_id}")
async def get_trip_location(trip_id: str, request: Request):
    """Get latest driver location for a trip"""
    user = await get_current_user(request)
    
    loc = await db.driver_locations.find_one({"trip_id": trip_id}, {"_id": 0})
    if not loc:
        raise HTTPException(status_code=404, detail="No location data for this trip")
    
    # Get driver name
    driver_user = None
    if loc.get("user_id"):
        driver_user = await db.users.find_one({"user_id": loc["user_id"]}, {"_id": 0})
    
    return LocationResponse(
        driver_id=loc["driver_id"],
        trip_id=loc["trip_id"],
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        timestamp=loc["timestamp"].isoformat() if isinstance(loc["timestamp"], datetime) else str(loc["timestamp"]),
        driver_name=driver_user.get("name") if driver_user else None
    )
