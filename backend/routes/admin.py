from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *

router = APIRouter()

@router.get("/dashboard/stats")
async def get_dashboard_stats(request: Request):
    user = await get_admin_user(request)
    
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    org_filter = get_org_filter(user)
    
    total_students = await db.students.count_documents({**org_filter})
    total_drivers = await db.drivers.count_documents({**org_filter})
    total_parents = await db.users.count_documents({"role": "parent", **org_filter})
    
    today_trips = await db.trips.count_documents({"date": today, **org_filter})
    active_trips = await db.trips.count_documents({"status": "in_progress", **org_filter})
    completed_today = await db.trips.count_documents({"date": today, "status": "completed", **org_filter})
    
    # Detailed breakdown for active trips
    active_trip_docs = await db.trips.find({"date": today, "status": {"$in": ["scheduled", "in_progress"]}, **org_filter}, {"_id": 0}).to_list(100)
    active_trip_ids = [t["trip_id"] for t in active_trip_docs]
    active_assignments = await db.trip_assignments.find({"trip_id": {"$in": active_trip_ids}}, {"_id": 0}).to_list(1000) if active_trip_ids else []
    
    pending_pickup = sum(1 for a in active_assignments if a["status"] == "pending")
    pending_dropoff = sum(1 for a in active_assignments if a["status"] == "picked_up")
    
    return {
        "total_students": total_students,
        "total_drivers": total_drivers,
        "total_parents": total_parents,
        "today_trips": today_trips,
        "active_trips": active_trips,
        "completed_today": completed_today,
        "pending_pickup": pending_pickup,
        "pending_dropoff": pending_dropoff
    }


@router.post("/seed")
async def seed_database(request: Request):
    """Seed database with demo data - for development only"""
    
    # Create or get demo org
    demo_org = await db.organizations.find_one({"name": "Demo Academy"}, {"_id": 0})
    if demo_org:
        org_id = demo_org["org_id"]
        if not demo_org.get("invite_code"):
            await db.organizations.update_one({"org_id": org_id}, {"$set": {"invite_code": "NR-DEMO"}})
    else:
        org_id = f"org_{uuid.uuid4().hex[:12]}"
        await db.organizations.insert_one({
            "org_id": org_id,
            "name": "Demo Academy",
            "owner_id": None,  # Will be set below
            "invite_code": "NR-DEMO",
            "created_at": datetime.now(timezone.utc)
        })
    
    # Helper: create or get existing user (preserves user_id on re-seed)
    async def upsert_user(email, name, phone, role, password):
        existing = await db.users.find_one({"email": email}, {"_id": 0})
        if existing:
            # Update password and role but keep user_id stable
            await db.users.update_one(
                {"email": email},
                {"$set": {
                    "name": name,
                    "phone": phone,
                    "role": role,
                    "password_hash": hash_password(password),
                    "org_id": org_id,
                    "approval_status": "approved",
                }}
            )
            return existing["user_id"]
        else:
            uid = f"user_{uuid.uuid4().hex[:12]}"
            await db.users.insert_one({
                "user_id": uid,
                "email": email,
                "name": name,
                "phone": phone,
                "role": role,
                "password_hash": hash_password(password),
                "profile_image": None,
                "org_id": org_id,
                "created_at": datetime.now(timezone.utc)
            })
            return uid
    
    admin_id = await upsert_user("admin@academy.com", "Admin User", "+1234567890", "admin", "admin123")
    # Firebase Test Lab / Robo demo account (mirrors admin permissions)
    demo_admin_id = await upsert_user("admin@demo.com", "Demo Admin", "+1234567891", "admin", "admin123")
    driver_user_id = await upsert_user("driver@academy.com", "John Driver", "+1987654321", "driver", "driver123")
    parent_id = await upsert_user("parent@academy.com", "Jane Parent", "+1555555555", "parent", "parent123")
    
    # Set org owner
    await db.organizations.update_one({"org_id": org_id}, {"$set": {"owner_id": admin_id}})
    
    # Migrate legacy data: set org_id on all records missing it
    await db.students.update_many({"org_id": {"$exists": False}}, {"$set": {"org_id": org_id}})
    await db.students.update_many({"org_id": None}, {"$set": {"org_id": org_id}})
    await db.drivers.update_many({"org_id": {"$exists": False}}, {"$set": {"org_id": org_id}})
    await db.drivers.update_many({"org_id": None}, {"$set": {"org_id": org_id}})
    await db.trips.update_many({"org_id": {"$exists": False}}, {"$set": {"org_id": org_id}})
    await db.trips.update_many({"org_id": None}, {"$set": {"org_id": org_id}})
    await db.notifications.update_many({"org_id": {"$exists": False}}, {"$set": {"org_id": org_id}})
    
    # Create / update driver profile
    existing_driver = await db.drivers.find_one({"user_id": driver_user_id}, {"_id": 0})
    driver_id = existing_driver["driver_id"] if existing_driver else f"driver_{uuid.uuid4().hex[:12]}"
    await db.drivers.update_one(
        {"user_id": driver_user_id},
        {
            "$set": {
                "driver_id": driver_id,
                "user_id": driver_user_id,
                "vehicle_type": "Van",
                "license_plate": "ABC-1234",
                "capacity": 8,
                "status": "active",
                "org_id": org_id,
                "created_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )
    
    # Create students (only if none exist for parent)
    existing_students = await db.students.count_documents({"parent_id": parent_id})
    if existing_students == 0:
        for name, age, notes, plat, plng, dlat, dlng in [
            ("Tommy", 10, "Has asthma, carries inhaler", 40.7580, -73.9855, 40.7484, -73.9856),
            ("Sarah", 8, None, 40.7614, -73.9776, 40.7484, -73.9856),
        ]:
            await db.students.insert_one({
                "student_id": f"student_{uuid.uuid4().hex[:12]}",
                "name": name,
                "age": age,
                "parent_id": parent_id,
                "pickup_address": "123 Home Street, City",
                "dropoff_address": "456 Academy Road, City",
                "pickup_lat": plat,
                "pickup_lng": plng,
                "dropoff_lat": dlat,
                "dropoff_lng": dlng,
                "notes": notes,
                "org_id": org_id,
                "created_at": datetime.now(timezone.utc)
            })
    
    return {
        "message": "Database seeded successfully",
        "org_id": org_id,
        "org_name": "Demo Academy",
        "credentials": {
            "admin": {"email": "admin@academy.com", "password": "admin123"},
            "driver": {"email": "driver@academy.com", "password": "driver123"},
            "parent": {"email": "parent@academy.com", "password": "parent123"}
        }
    }


@router.get("/org/invite-code")
async def get_invite_code(request: Request):
    """Get the org invite code (admin only)"""
    user = await get_admin_user(request)
    if not user.org_id:
        raise HTTPException(status_code=400, detail="No organization found")
    
    org = await db.organizations.find_one({"org_id": user.org_id}, {"_id": 0})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Generate invite code if missing
    if not org.get("invite_code"):
        import random, string
        invite_code = "NR-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        await db.organizations.update_one({"org_id": user.org_id}, {"$set": {"invite_code": invite_code}})
        org["invite_code"] = invite_code
    
    return {"invite_code": org["invite_code"], "org_name": org.get("name")}

@router.post("/org/regenerate-code")
async def regenerate_invite_code(request: Request):
    """Regenerate the org invite code (admin only)"""
    user = await get_admin_user(request)
    import random, string
    new_code = "NR-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    await db.organizations.update_one({"org_id": user.org_id}, {"$set": {"invite_code": new_code}})
    return {"invite_code": new_code}

@router.get("/org/pending-members")
async def get_pending_members(request: Request):
    """Get pending driver/parent approvals (admin only)"""
    user = await get_admin_user(request)
    
    pending = await db.users.find(
        {"org_id": user.org_id, "approval_status": "pending"},
        {"_id": 0, "password_hash": 0}
    ).to_list(100)
    
    # Enrich drivers with vehicle details
    for member in pending:
        if member.get("role") == "driver":
            vehicle = await db.driver_vehicles.find_one({"user_id": member["user_id"]}, {"_id": 0})
            member["vehicle"] = vehicle
    
    return pending

@router.get("/org/members")
async def get_org_members(request: Request):
    """Get all org members (admin only)"""
    user = await get_admin_user(request)
    
    members = await db.users.find(
        {"org_id": user.org_id, "role": {"$ne": "admin"}},
        {"_id": 0, "password_hash": 0}
    ).to_list(500)
    
    for member in members:
        if member.get("role") == "driver":
            vehicle = await db.driver_vehicles.find_one({"user_id": member["user_id"]}, {"_id": 0})
            member["vehicle"] = vehicle
    
    return members

@router.put("/org/members/{member_id}/approve")
async def approve_member(member_id: str, request: Request):
    """Approve a pending member (admin only)"""
    user = await get_admin_user(request)
    
    member = await db.users.find_one({"user_id": member_id, "org_id": user.org_id}, {"_id": 0})
    if not member:
        raise HTTPException(status_code=404, detail="Member not found in your organization")
    
    await db.users.update_one(
        {"user_id": member_id},
        {"$set": {"approval_status": "approved"}}
    )
    
    # If driver, create/activate driver profile
    if member.get("role") == "driver":
        vehicle = await db.driver_vehicles.find_one({"user_id": member_id}, {"_id": 0})
        existing_driver = await db.drivers.find_one({"user_id": member_id})
        if not existing_driver:
            await db.drivers.insert_one({
                "driver_id": f"driver_{uuid.uuid4().hex[:12]}",
                "user_id": member_id,
                "org_id": user.org_id,
                "vehicle_type": f"{vehicle.get('make', '')} {vehicle.get('model', '')}" if vehicle else "Pending",
                "license_plate": vehicle.get("license_plate", "Pending") if vehicle else "Pending",
                "capacity": 4,
                "status": "active",
                "created_at": datetime.now(timezone.utc)
            })
        else:
            await db.drivers.update_one(
                {"user_id": member_id},
                {"$set": {
                    "status": "active",
                    "org_id": user.org_id,
                    "vehicle_type": f"{vehicle.get('make', '')} {vehicle.get('model', '')}" if vehicle else "Pending",
                    "license_plate": vehicle.get("license_plate", "Pending") if vehicle else "Pending",
                }}
            )
    
    # Notify the member
    await create_notification(member_id, "You have been approved! Welcome to the team.", "alert")
    
    return {"message": f"Member approved successfully"}

@router.put("/org/members/{member_id}/decline")
async def decline_member(member_id: str, request: Request):
    """Decline a pending member (admin only)"""
    user = await get_admin_user(request)
    
    await db.users.update_one(
        {"user_id": member_id, "org_id": user.org_id},
        {"$set": {"approval_status": "declined"}}
    )
    
    await create_notification(member_id, "Your membership request has been declined.", "alert")
    
    return {"message": "Member declined"}



@router.post("/issues")
async def report_issue(issue: IssueReport, request: Request):
    """Any user can report an issue"""
    user = await get_current_user(request)
    
    issue_doc = {
        "issue_id": f"issue_{uuid.uuid4().hex[:12]}",
        "user_id": user.user_id,
        "user_name": user.name,
        "user_role": user.role,
        "user_email": user.email,
        "org_id": user.org_id,
        "category": issue.category,
        "description": issue.description,
        "trip_id": issue.trip_id,
        "status": "open",
        "created_at": datetime.now(timezone.utc)
    }
    await db.issues.insert_one(issue_doc)
    
    # Notify org admins
    if user.org_id:
        admins = await db.users.find({"org_id": user.org_id, "role": "admin"}, {"_id": 0}).to_list(10)
        for admin in admins:
            await create_notification(
                admin["user_id"],
                f"New issue reported by {user.name} ({user.role}): {issue.category}",
                "alert"
            )
    
    return {"message": "Issue reported successfully", "issue_id": issue_doc["issue_id"]}

@router.get("/issues")
async def get_issues(request: Request, status: Optional[str] = None):
    """Admin views all issues for their org"""
    user = await get_admin_user(request)
    org_filter = get_org_filter(user)
    
    query = {**org_filter}
    if status:
        query["status"] = status
    
    issues = await db.issues.find(query, {"_id": 0}).sort("created_at", -1).to_list(200)
    for i in issues:
        if isinstance(i.get("created_at"), datetime):
            i["created_at"] = i["created_at"].isoformat()
    return issues

@router.put("/issues/{issue_id}/resolve")
async def resolve_issue(issue_id: str, request: Request):
    """Admin resolves an issue"""
    user = await get_admin_user(request)
    
    result = await db.issues.update_one(
        {"issue_id": issue_id},
        {"$set": {"status": "resolved", "resolved_by": user.user_id, "resolved_at": datetime.now(timezone.utc)}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Issue not found")
    
    # Notify reporter
    issue = await db.issues.find_one({"issue_id": issue_id}, {"_id": 0})
    if issue:
        await create_notification(issue["user_id"], "Your reported issue has been resolved.", "alert")
    
    return {"message": "Issue resolved"}

# ======================== ORG INFO ========================

@router.get("/org/info")
async def get_org_info(request: Request):
    """Get current user's organization info"""
    user = await get_current_user(request)
    
    if not user.org_id:
        return {"org_id": None, "name": None, "message": "Not part of any organization"}
    
    org = await db.organizations.find_one({"org_id": user.org_id}, {"_id": 0})
    if not org:
        return {"org_id": user.org_id, "name": "Unknown", "message": "Organization not found"}
    
    return {
        "org_id": org["org_id"],
        "name": org["name"],
        "owner_id": org.get("owner_id"),
        "created_at": org["created_at"].isoformat() if isinstance(org.get("created_at"), datetime) else str(org.get("created_at", ""))
    }
