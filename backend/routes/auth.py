from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *
import httpx
import bcrypt
from jose import jwt, JWTError
from database import ACCESS_TOKEN_EXPIRE_DAYS
router = APIRouter()

@router.post("/auth/register", response_model=TokenResponse)
async def register(user_data: UserCreate, response: Response):
    # Check if user exists
    existing = await db.users.find_one({"email": user_data.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    hashed_password = hash_password(user_data.password)
    
    # If registering as admin, create an organization
    org_id = None
    approval_status = "approved"  # admins are auto-approved
    if user_data.role == "admin":
        org_id = f"org_{uuid.uuid4().hex[:12]}"
        import random, string
        invite_code = "NR-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        await db.organizations.insert_one({
            "org_id": org_id,
            "name": user_data.organization_name or f"{user_data.name}'s Academy",
            "owner_id": user_id,
            "invite_code": invite_code,
            "created_at": datetime.now(timezone.utc)
        })
    elif user_data.invite_code:
        # Driver or Parent joining via invite code
        org = await db.organizations.find_one({"invite_code": user_data.invite_code.strip().upper()})
        if not org:
            raise HTTPException(status_code=400, detail="Invalid invite code. Please check with your academy admin.")
        org_id = org["org_id"]
        approval_status = "pending"  # needs admin approval
    
    user_doc = {
        "user_id": user_id,
        "email": user_data.email,
        "name": user_data.name,
        "phone": user_data.phone,
        "role": user_data.role,
        "password_hash": hashed_password,
        "profile_image": None,
        "org_id": org_id,
        "approval_status": approval_status,
        "has_seen_onboarding": False,
        "organization_name": user_data.organization_name if user_data.role == "admin" else None,
        "manager_name": user_data.manager_name if user_data.role == "admin" else None,
        "created_at": datetime.now(timezone.utc)
    }
    await db.users.insert_one(user_doc)
    
    # If registering as driver, auto-create a driver profile placeholder
    if user_data.role == "driver":
        await db.drivers.insert_one({
            "driver_id": f"driver_{uuid.uuid4().hex[:12]}",
            "user_id": user_id,
            "org_id": org_id,
            "vehicle_type": "Pending",
            "license_plate": "Pending",
            "capacity": 4,
            "status": "inactive",
            "created_at": datetime.now(timezone.utc)
        })
    
    # Create access token
    access_token = create_access_token({"user_id": user_id})
    
    # Set cookie
    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=ACCESS_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path="/"
    )
    
    user = User(
        user_id=user_id,
        email=user_data.email,
        name=user_data.name,
        phone=user_data.phone,
        role=user_data.role,
        profile_image=None,
        org_id=org_id,
        approval_status=approval_status,
        created_at=user_doc["created_at"]
    )
    
    return TokenResponse(access_token=access_token, user=user)

@router.post("/auth/login", response_model=TokenResponse)
async def login(credentials: UserLogin, response: Response):
    user_doc = await db.users.find_one({"email": credentials.email}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    if not user_doc.get("password_hash"):
        raise HTTPException(status_code=401, detail="Please use Google login for this account")
    
    if not verify_password(credentials.password, user_doc["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token = create_access_token({"user_id": user_doc["user_id"]})
    
    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=ACCESS_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path="/"
    )
    
    user = User(
        user_id=user_doc["user_id"],
        email=user_doc["email"],
        name=user_doc["name"],
        phone=user_doc.get("phone"),
        role=user_doc["role"],
        profile_image=user_doc.get("profile_image"),
        org_id=user_doc.get("org_id"),
        approval_status=user_doc.get("approval_status", "approved"),
        has_seen_onboarding=user_doc.get("has_seen_onboarding", True),
        created_at=user_doc["created_at"]
    )
    
    return TokenResponse(access_token=access_token, user=user)

@router.post("/auth/session")
async def exchange_session(request: Request, response: Response):
    """Exchange Emergent OAuth session_id for user data and session token"""
    body = await request.json()
    session_id = body.get("session_id")
    
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    
    # Call Emergent Auth to get session data
    async with httpx.AsyncClient() as http_client:
        try:
            auth_response = await http_client.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": session_id}
            )
            if auth_response.status_code != 200:
                raise HTTPException(status_code=401, detail="Invalid session")
            
            session_data = auth_response.json()
        except Exception as e:
            logger.error(f"Error exchanging session: {e}")
            raise HTTPException(status_code=500, detail="Auth service error")
    
    email = session_data.get("email")
    name = session_data.get("name")
    picture = session_data.get("picture")
    session_token = session_data.get("session_token")
    
    # Find or create user
    user_doc = await db.users.find_one({"email": email}, {"_id": 0})
    
    if user_doc:
        user_id = user_doc["user_id"]
        # Update profile image if changed
        if picture and picture != user_doc.get("profile_image"):
            await db.users.update_one(
                {"user_id": user_id},
                {"$set": {"profile_image": picture, "name": name}}
            )
    else:
        # Create new user (default role: parent)
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user_doc = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "phone": None,
            "role": "parent",  # Default role for Google OAuth users
            "profile_image": picture,
            "org_id": None,
            "approval_status": "approved",
            "created_at": datetime.now(timezone.utc)
        }
        await db.users.insert_one(user_doc)
    
    # Store session
    expires_at = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    await db.user_sessions.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "session_token": session_token,
                "expires_at": expires_at,
                "created_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )
    
    # Set cookie
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=ACCESS_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path="/"
    )
    
    # Get updated user
    user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    
    return {
        "user": User(**user_doc),
        "session_token": session_token
    }

@router.get("/auth/me", response_model=User)
async def get_me(request: Request):
    user = await get_current_user(request)
    return user

@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    session_token = request.cookies.get("session_token")
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    
    response.delete_cookie(key="session_token", path="/")
    return {"message": "Logged out successfully"}

@router.put("/auth/profile")
async def update_profile(profile_data: ProfileUpdate, request: Request):
    """Update current user's profile (name, phone)"""
    user = await get_current_user(request)
    
    update_fields = {}
    if profile_data.name is not None and profile_data.name.strip():
        update_fields["name"] = profile_data.name.strip()
    if profile_data.phone is not None:
        update_fields["phone"] = profile_data.phone.strip() if profile_data.phone.strip() else None
    
    if not update_fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": update_fields}
    )
    
    updated = await db.users.find_one({"user_id": user.user_id}, {"_id": 0, "password_hash": 0})
    return User(**updated)

@router.put("/auth/onboarding-complete")
async def mark_onboarding_complete(request: Request):
    """Mark onboarding as seen for the current user"""
    user = await get_current_user(request)
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"has_seen_onboarding": True}}
    )
    return {"message": "Onboarding completed"}


@router.get("/users", response_model=List[User])
async def get_users(request: Request, role: Optional[str] = None):
    await get_admin_user(request)
    
    query = {}
    if role:
        query["role"] = role
    
    users = await db.users.find(query, {"_id": 0, "password_hash": 0}).to_list(1000)
    return [User(**u) for u in users]

@router.put("/users/{user_id}/role")
async def update_user_role(user_id: str, request: Request):
    await get_admin_user(request)
    body = await request.json()
    new_role = body.get("role")
    
    if new_role not in ["admin", "driver", "parent"]:
        raise HTTPException(status_code=400, detail="Invalid role")
    
    result = await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"role": new_role}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {"message": "Role updated successfully"}


@router.put("/auth/switch-role")
async def switch_role(request: Request):
    """Switch current user's role — for demo/testing purposes"""
    user = await get_current_user(request)
    body = await request.json()
    new_role = body.get("role")
    
    if new_role not in ["admin", "driver", "parent"]:
        raise HTTPException(status_code=400, detail="Invalid role. Use: admin, driver, parent")
    
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"role": new_role}}
    )
    
    # If switching to driver, create a driver profile if needed
    if new_role == "driver":
        existing = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
        if not existing:
            await db.drivers.insert_one({
                "driver_id": f"driver_{uuid.uuid4().hex[:12]}",
                "user_id": user.user_id,
                "vehicle_type": "Car",
                "license_plate": "DEMO-0000",
                "capacity": 4,
                "status": "active",
                "created_at": datetime.now(timezone.utc)
            })
    
    updated = await db.users.find_one({"user_id": user.user_id}, {"_id": 0})
    return User(**updated)
