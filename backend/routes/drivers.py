from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *

router = APIRouter()

@router.post("/drivers", response_model=Driver)
async def create_driver(driver_data: DriverCreate, request: Request):
    user = await get_admin_user(request)
    
    # Verify user exists and update their role
    user_doc = await db.users.find_one({"user_id": driver_data.user_id}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Update user role to driver and link to org
    await db.users.update_one(
        {"user_id": driver_data.user_id},
        {"$set": {"role": "driver", "org_id": user.org_id, "approval_status": "approved"}}
    )
    
    # Upsert driver record (handles case where driver already exists)
    existing_driver = await db.drivers.find_one({"user_id": driver_data.user_id})
    if existing_driver:
        driver_id = existing_driver["driver_id"]
        await db.drivers.update_one(
            {"user_id": driver_data.user_id},
            {"$set": {
                **driver_data.dict(),
                "org_id": user.org_id,
                "status": "active",
            }}
        )
        driver_doc = {**existing_driver, **driver_data.dict(), "org_id": user.org_id, "driver_id": driver_id}
    else:
        driver_id = f"driver_{uuid.uuid4().hex[:12]}"
        driver_doc = {
            "driver_id": driver_id,
            **driver_data.dict(),
            "org_id": user.org_id,
            "created_at": datetime.now(timezone.utc)
        }
        await db.drivers.insert_one(driver_doc)
    
    return Driver(
        **driver_doc,
        user_name=user_doc.get("name"),
        user_email=user_doc.get("email"),
        user_phone=user_doc.get("phone")
    )

@router.get("/drivers", response_model=List[Driver])
async def get_drivers(request: Request):
    user = await get_current_user(request)
    org_filter = get_org_filter(user)
    
    drivers = await db.drivers.find({**org_filter}, {"_id": 0}).to_list(100)
    
    if not drivers:
        return []
    
    # Batch fetch all users for these drivers
    user_ids = [d["user_id"] for d in drivers]
    user_docs = await db.users.find({"user_id": {"$in": user_ids}}, {"_id": 0}).to_list(100)
    user_lookup = {u["user_id"]: u for u in user_docs}
    
    result = []
    for d in drivers:
        u = user_lookup.get(d["user_id"])
        driver = Driver(
            **d,
            user_name=u.get("name") if u else None,
            user_email=u.get("email") if u else None,
            user_phone=u.get("phone") if u else None
        )
        result.append(driver)
    
    return result

@router.get("/drivers/{driver_id}", response_model=Driver)
async def get_driver(driver_id: str, request: Request):
    await get_current_user(request)
    
    driver_doc = await db.drivers.find_one({"driver_id": driver_id}, {"_id": 0})
    if not driver_doc:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    user_doc = await db.users.find_one({"user_id": driver_doc["user_id"]}, {"_id": 0})
    
    return Driver(
        **driver_doc,
        user_name=user_doc.get("name") if user_doc else None,
        user_email=user_doc.get("email") if user_doc else None,
        user_phone=user_doc.get("phone") if user_doc else None
    )

@router.get("/drivers/me/profile", response_model=Driver)
async def get_my_driver_profile(request: Request):
    user = await get_driver_user(request)
    
    driver_doc = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
    if not driver_doc:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    return Driver(
        **driver_doc,
        user_name=user.name,
        user_email=user.email,
        user_phone=user.phone
    )

@router.put("/drivers/{driver_id}", response_model=Driver)
async def update_driver(driver_id: str, driver_data: DriverBase, request: Request):
    await get_admin_user(request)
    
    result = await db.drivers.update_one(
        {"driver_id": driver_id},
        {"$set": driver_data.dict()}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    return await get_driver(driver_id, request)

@router.delete("/drivers/{driver_id}")
async def delete_driver(driver_id: str, request: Request):
    await get_admin_user(request)
    
    driver_doc = await db.drivers.find_one({"driver_id": driver_id}, {"_id": 0})
    if not driver_doc:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    # Revert user role to parent
    await db.users.update_one(
        {"user_id": driver_doc["user_id"]},
        {"$set": {"role": "parent"}}
    )
    
    await db.drivers.delete_one({"driver_id": driver_id})
    
    return {"message": "Driver deleted"}



@router.put("/users/me/photo")
async def upload_profile_photo(photo_data: PhotoUpload, request: Request):
    """Upload/update profile photo (base64 encoded)"""
    user = await get_current_user(request)
    
    # Validate base64 — just check it's not empty and reasonable size (<5MB)
    if len(photo_data.photo) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Photo too large (max 5MB)")
    
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"profile_image": photo_data.photo}}
    )
    
    return {"message": "Photo updated successfully"}

@router.delete("/users/me/photo")
async def delete_profile_photo(request: Request):
    user = await get_current_user(request)
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"profile_image": None}}
    )
    return {"message": "Photo removed"}



@router.put("/driver/vehicle")
async def update_vehicle_details(vehicle: VehicleDetails, request: Request):
    """Driver updates their vehicle details"""
    user = await get_current_user(request)
    if user.role != "driver":
        raise HTTPException(status_code=403, detail="Only drivers can update vehicle details")
    
    await db.driver_vehicles.update_one(
        {"user_id": user.user_id},
        {"$set": {
            "user_id": user.user_id,
            **vehicle.dict(),
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )
    
    return {"message": "Vehicle details saved", "vehicle": vehicle.dict()}

@router.get("/driver/vehicle")
async def get_vehicle_details(request: Request):
    """Get driver's vehicle details"""
    user = await get_current_user(request)
    
    vehicle = await db.driver_vehicles.find_one({"user_id": user.user_id}, {"_id": 0})
    return vehicle or {}
