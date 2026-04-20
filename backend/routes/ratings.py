from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *

router = APIRouter()

@router.post("/ratings")
async def create_rating(rating_data: RatingCreate, request: Request):
    """Create a driver rating (parent only, one per driver)"""
    user = await get_current_user(request)
    if user.role != "parent":
        raise HTTPException(status_code=403, detail="Only parents can rate drivers")
    
    # Check if already rated this driver
    existing = await db.ratings.find_one({
        "parent_id": user.user_id,
        "driver_id": rating_data.driver_id,
    })
    if existing:
        raise HTTPException(status_code=400, detail="You have already rated this driver")
    
    # Verify driver exists
    driver = await db.drivers.find_one({"driver_id": rating_data.driver_id}, {"_id": 0})
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    # Get driver user name
    driver_user = await db.users.find_one({"user_id": driver["user_id"]}, {"_id": 0})
    
    rating_doc = {
        "rating_id": f"rating_{uuid.uuid4().hex[:12]}",
        "driver_id": rating_data.driver_id,
        "driver_name": driver_user.get("name") if driver_user else "Unknown",
        "parent_id": user.user_id,
        "parent_name": user.name,
        "trip_id": rating_data.trip_id,
        "rating": rating_data.rating,
        "comment": rating_data.comment,
        "org_id": user.org_id,
        "created_at": datetime.now(timezone.utc)
    }
    await db.ratings.insert_one(rating_doc)
    
    return {"message": "Rating submitted", "rating_id": rating_doc["rating_id"]}

@router.get("/ratings/driver/{driver_id}")
async def get_driver_ratings(driver_id: str, request: Request):
    """Get all ratings for a driver"""
    await get_current_user(request)
    
    ratings = await db.ratings.find({"driver_id": driver_id}, {"_id": 0}).sort("created_at", -1).to_list(100)
    
    for r in ratings:
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
    
    # Calculate average
    avg = 0
    if ratings:
        avg = round(sum(r["rating"] for r in ratings) / len(ratings), 1)
    
    return {
        "driver_id": driver_id,
        "average_rating": avg,
        "total_ratings": len(ratings),
        "ratings": ratings
    }

@router.get("/ratings/my-ratings")
async def get_my_ratings(request: Request):
    """Get all ratings submitted by current parent"""
    user = await get_current_user(request)
    
    ratings = await db.ratings.find({"parent_id": user.user_id}, {"_id": 0}).to_list(100)
    
    for r in ratings:
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
    
    # Return as a map of driver_id -> rating for easy lookup
    rated_drivers = {r["driver_id"]: r for r in ratings}
    
    return {"ratings": ratings, "rated_drivers": rated_drivers}
