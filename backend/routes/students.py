from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *

router = APIRouter()

@router.post("/students", response_model=Student)
async def create_student(student_data: StudentCreate, request: Request):
    user = await get_current_user(request)
    
    # Resolve parent_id
    parent_id = student_data.parent_id
    
    # Admin can link child to parent by email
    if user.role == "admin" and student_data.parent_email:
        parent_user = await db.users.find_one({"email": student_data.parent_email}, {"_id": 0})
        
        if not parent_user:
            # Create placeholder parent account
            import random, string
            parent_id = f"user_{uuid.uuid4().hex[:12]}"
            temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
            
            parent_user_doc = {
                "user_id": parent_id,
                "email": student_data.parent_email,
                "name": student_data.parent_email.split('@')[0].title(),
                "phone": student_data.parent_phone or None,
                "role": "parent",
                "password_hash": hash_password(temp_password),
                "profile_image": None,
                "org_id": user.org_id,
                "approval_status": "approved",
                "created_at": datetime.now(timezone.utc)
            }
            await db.users.insert_one(parent_user_doc)
            
            # Get org invite code for the SMS
            org = await db.organizations.find_one({"org_id": user.org_id}, {"_id": 0})
            org_name = org.get("name", "the academy") if org else "the academy"
            invite_code = org.get("invite_code", "") if org else ""
            
            # Send SMS invite via Twilio if phone provided
            if student_data.parent_phone and twilio_client:
                sms_message = (
                    f"Hi! You've been invited to join {org_name} on NestRoute. "
                    f"Your child {student_data.name} has been added.\n\n"
                    f"Download NestRoute and sign up with:\n"
                    f"Email: {student_data.parent_email}\n"
                    f"Temp Password: {temp_password}\n"
                    f"Invite Code: {invite_code}\n\n"
                    f"Please change your password after first login."
                )
                await send_sms(student_data.parent_phone, sms_message, None, "parent_invite")
            
            # Create notification for admin confirmation
            await create_notification(
                user.user_id,
                f"Parent account created for {student_data.parent_email}. "
                + (f"SMS invite sent to {student_data.parent_phone}." if student_data.parent_phone else "Share invite code manually."),
                "alert"
            )
        else:
            parent_id = parent_user["user_id"]
            # Link parent to admin's org if not already linked
            if user.org_id and not parent_user.get("org_id"):
                await db.users.update_one({"user_id": parent_id}, {"$set": {"org_id": user.org_id}})
    elif user.role == "parent":
        parent_id = user.user_id
    elif user.role == "admin" and not parent_id and not student_data.parent_email:
        raise HTTPException(status_code=400, detail="Please provide parent email to link this child")
    
    if not parent_id:
        raise HTTPException(status_code=400, detail="Parent ID or parent email required")
    
    student_id = f"student_{uuid.uuid4().hex[:12]}"
    student_doc = {
        "student_id": student_id,
        "name": student_data.name,
        "age": student_data.age,
        "parent_id": parent_id,
        "pickup_address": student_data.pickup_address,
        "dropoff_address": student_data.dropoff_address,
        "notes": student_data.notes,
        "org_id": user.org_id,
        "created_at": datetime.now(timezone.utc)
    }
    await db.students.insert_one(student_doc)
    
    return Student(**student_doc)

@router.get("/students")
async def get_students(request: Request):
    user = await get_current_user(request)
    org_filter = get_org_filter(user)
    
    if user.role == "admin":
        students = await db.students.find({**org_filter}, {"_id": 0}).to_list(1000)
    elif user.role == "parent":
        students = await db.students.find({"parent_id": user.user_id, **org_filter}, {"_id": 0}).to_list(100)
    else:
        # Drivers see students assigned to their trips
        driver = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
        if not driver:
            return []
        
        trips = await db.trips.find({"driver_id": driver["driver_id"]}, {"_id": 0}).to_list(100)
        trip_ids = [t["trip_id"] for t in trips]
        
        assignments = await db.trip_assignments.find({"trip_id": {"$in": trip_ids}}, {"_id": 0}).to_list(1000)
        student_ids = list(set([a["student_id"] for a in assignments]))
        
        students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(100)
    
    # Enrich with parent phone
    valid_students = [s for s in students if s.get("parent_id")]
    parent_ids = list(set(s["parent_id"] for s in valid_students))
    parents = await db.users.find({"user_id": {"$in": parent_ids}}, {"_id": 0, "user_id": 1, "phone": 1, "name": 1, "email": 1}).to_list(500)
    parent_map = {p["user_id"]: p for p in parents}
    
    enriched = []
    for s in valid_students:
        parent = parent_map.get(s["parent_id"], {})
        s["parent_phone"] = parent.get("phone")
        s["parent_name"] = parent.get("name")
        s["parent_email"] = parent.get("email")
        enriched.append(s)
    
    return enriched

@router.get("/students/{student_id}", response_model=Student)
async def get_student(student_id: str, request: Request):
    user = await get_current_user(request)
    
    student_doc = await db.students.find_one({"student_id": student_id}, {"_id": 0})
    if not student_doc:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Check access
    if user.role == "parent" and student_doc["parent_id"] != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return Student(**student_doc)

@router.put("/students/{student_id}", response_model=Student)
async def update_student(student_id: str, student_data: StudentBase, request: Request):
    user = await get_current_user(request)
    
    student_doc = await db.students.find_one({"student_id": student_id}, {"_id": 0})
    if not student_doc:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Check access
    if user.role == "parent" and student_doc["parent_id"] != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    elif user.role not in ["admin", "parent"]:
        raise HTTPException(status_code=403, detail="Cannot update student")
    
    await db.students.update_one(
        {"student_id": student_id},
        {"$set": student_data.dict()}
    )
    
    updated = await db.students.find_one({"student_id": student_id}, {"_id": 0})
    return Student(**updated)

@router.delete("/students/{student_id}")
async def delete_student(student_id: str, request: Request):
    await get_admin_user(request)
    
    result = await db.students.delete_one({"student_id": student_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Also remove from trip assignments
    await db.trip_assignments.delete_many({"student_id": student_id})
    
    return {"message": "Student deleted"}
