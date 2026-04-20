"""Shared helper functions used across multiple route modules."""
from fastapi import HTTPException, Request
from datetime import datetime, timezone, timedelta
from typing import List, Optional
import uuid
import bcrypt
import math
from jose import jwt, JWTError

from database import (
    db, manager, logger,
    SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_DAYS,
    twilio_client, TWILIO_MESSAGING_SERVICE_SID,
    SUBSCRIPTION_TIERS, PROXIMITY_THRESHOLD_METERS,
)
from models import User


# ======================== AUTH HELPERS ========================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(request: Request) -> User:
    session_token = request.cookies.get("session_token")
    if not session_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            session_token = auth_header.split(" ")[1]
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(session_token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if user_id:
            user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
            if user_doc:
                return User(**user_doc)
    except JWTError:
        pass
    session_doc = await db.user_sessions.find_one({"session_token": session_token}, {"_id": 0})
    if session_doc:
        expires_at = session_doc.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Session expired")
        user_doc = await db.users.find_one({"user_id": session_doc["user_id"]}, {"_id": 0})
        if user_doc:
            return User(**user_doc)
    raise HTTPException(status_code=401, detail="Invalid token")

async def get_admin_user(request: Request) -> User:
    user = await get_current_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

async def get_driver_user(request: Request) -> User:
    user = await get_current_user(request)
    if user.role not in ["admin", "driver"]:
        raise HTTPException(status_code=403, detail="Driver access required")
    return user

def get_org_filter(user: User) -> dict:
    if user.org_id:
        return {"org_id": user.org_id}
    return {}


# ======================== GEO UTILITIES ========================

def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def check_proximity(driver_lat: float, driver_lng: float, target_lat: float, target_lng: float) -> dict:
    distance = haversine_distance(driver_lat, driver_lng, target_lat, target_lng)
    return {
        "distance_meters": round(distance, 1),
        "within_threshold": distance <= PROXIMITY_THRESHOLD_METERS,
        "threshold": PROXIMITY_THRESHOLD_METERS
    }

def optimize_route_nearest_neighbor(driver_lat: float, driver_lng: float, stops: list) -> list:
    if not stops:
        return []
    remaining = list(stops)
    ordered = []
    current_lat, current_lng = driver_lat, driver_lng
    while remaining:
        nearest = min(remaining, key=lambda s: haversine_distance(
            current_lat, current_lng, s["lat"], s["lng"]
        ))
        remaining.remove(nearest)
        ordered.append(nearest)
        current_lat, current_lng = nearest["lat"], nearest["lng"]
    return ordered


# ======================== NOTIFICATION HELPERS ========================

async def create_notification(user_id: str, message: str, notification_type: str = "info", trip_id: str = None):
    # Dedup: skip if identical notification exists within last 5 minutes
    dedup_filter = {
        "user_id": user_id,
        "message": message,
        "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(minutes=5)}
    }
    if trip_id:
        dedup_filter["trip_id"] = trip_id
    existing = await db.notifications.find_one(dedup_filter)
    if existing:
        logger.info(f"Notification deduped for {user_id}: {message[:40]}...")
        return existing

    notification = {
        "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
        "user_id": user_id,
        "message": message,
        "type": notification_type,
        "trip_id": trip_id,
        "read_status": False,
        "created_at": datetime.now(timezone.utc)
    }
    await db.notifications.insert_one(notification)
    await manager.send_personal_message({
        "type": "notification",
        "data": {
            "notification_id": notification["notification_id"],
            "message": message,
            "notification_type": notification_type,
            "trip_id": trip_id,
            "created_at": notification["created_at"].isoformat()
        }
    }, user_id)
    return notification

async def notify_parents_of_students(student_ids: List[str], message: str, notification_type: str = "trip_update", trip_id: str = None):
    students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(100)
    parent_ids = list(set([s["parent_id"] for s in students]))
    for parent_id in parent_ids:
        await create_notification(parent_id, message, notification_type, trip_id)


# ======================== SMS HELPERS ========================

async def send_sms(to_phone: str, message: str, trip_id: str = None, event_type: str = None, org_id: str = None):
    if not twilio_client or not to_phone:
        logger.info(f"SMS skipped: client={'yes' if twilio_client else 'no'}, phone={to_phone}")
        return False
    if trip_id and event_type:
        existing = await db.sms_logs.find_one({
            "trip_id": trip_id, "event_type": event_type, "phone": to_phone,
            "sent_at": {"$gte": datetime.now(timezone.utc) - timedelta(minutes=5)}
        })
        if existing:
            logger.info(f"SMS rate-limited: {event_type} for {to_phone}")
            return False
    try:
        msg = twilio_client.messages.create(
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            to=to_phone, body=message
        )
        await db.sms_logs.insert_one({
            "sms_id": f"sms_{uuid.uuid4().hex[:12]}",
            "phone": to_phone, "message": message, "trip_id": trip_id,
            "event_type": event_type, "org_id": org_id,
            "twilio_sid": msg.sid, "status": str(msg.status),
            "sent_at": datetime.now(timezone.utc)
        })
        logger.info(f"SMS sent: {event_type} to {to_phone} (SID: {msg.sid})")
        return True
    except Exception as e:
        logger.error(f"SMS send error to {to_phone}: {e}")
        return False

async def sms_notify_parents(student_ids: List[str], message: str, trip_id: str, event_type: str):
    admin_sub = await db.subscriptions.find_one({"status": "active", "sms_enabled": True})
    if not admin_sub:
        logger.info("SMS skipped: no active SMS-enabled subscription")
        return
    students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(100)
    parent_ids = list(set([s["parent_id"] for s in students]))
    for parent_id in parent_ids:
        parent = await db.users.find_one({"user_id": parent_id}, {"_id": 0})
        if parent and parent.get("phone"):
            await send_sms(parent["phone"], message, trip_id, event_type)


# ======================== AUDIT LOG HELPERS ========================

async def create_audit_log(trip_id: str, event_type: str, actor_id: str, details: dict = None):
    actor = await db.users.find_one({"user_id": actor_id}, {"_id": 0})
    log_entry = {
        "log_id": f"log_{uuid.uuid4().hex[:12]}",
        "trip_id": trip_id, "event_type": event_type,
        "actor_id": actor_id,
        "actor_name": actor.get("name") if actor else "System",
        "actor_role": actor.get("role") if actor else "system",
        "org_id": actor.get("org_id") if actor else None,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc)
    }
    await db.audit_logs.insert_one(log_entry)
    return log_entry


# ======================== SUBSCRIPTION HELPERS ========================

async def get_admin_subscription(user_id: str):
    sub = await db.subscriptions.find_one(
        {"user_id": user_id, "status": {"$in": ["active", "trialing"]}}, {"_id": 0}
    )
    if sub:
        return sub
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if user and user.get("org_id"):
        sub = await db.subscriptions.find_one(
            {"org_id": user["org_id"], "status": {"$in": ["active", "trialing"]}}, {"_id": 0}
        )
    return sub

async def check_subscription_limits(user_id: str, resource_type: str):
    sub = await get_admin_subscription(user_id)
    if not sub:
        tier_info = {"student_limit": 5, "driver_limit": 1}
    else:
        tier = sub.get("tier", "starter")
        tier_info = SUBSCRIPTION_TIERS.get(tier, SUBSCRIPTION_TIERS["starter"])
    if resource_type == "student":
        limit = tier_info.get("student_limit", 5)
        if limit == -1:
            return
        user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
        org_q = {"org_id": user_doc["org_id"]} if user_doc and user_doc.get("org_id") else {}
        current_count = await db.students.count_documents(org_q)
        if current_count >= limit:
            raise HTTPException(status_code=403, detail=f"Student limit reached ({limit}). Please upgrade your subscription.")
    elif resource_type == "driver":
        limit = tier_info.get("driver_limit", 1)
        if limit == -1:
            return
        user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
        org_q = {"org_id": user_doc["org_id"]} if user_doc and user_doc.get("org_id") else {}
        current_count = await db.drivers.count_documents(org_q)
        if current_count >= limit:
            raise HTTPException(status_code=403, detail=f"Driver limit reached ({limit}). Please upgrade your subscription.")
