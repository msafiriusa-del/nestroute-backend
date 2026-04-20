from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *

router = APIRouter()

@router.get("/audit-logs")
async def get_audit_logs(request: Request, trip_id: Optional[str] = None, limit: int = 50):
    """Get audit logs - admin only, org-scoped"""
    user = await get_admin_user(request)
    org_filter = get_org_filter(user)
    
    query = {**org_filter}
    if trip_id:
        query["trip_id"] = trip_id
    
    logs = await db.audit_logs.find(query, {"_id": 0}).sort("timestamp", -1).to_list(limit)
    
    # Convert datetime to ISO string
    for log in logs:
        if isinstance(log.get("timestamp"), datetime):
            log["timestamp"] = log["timestamp"].isoformat()
    
    return logs

@router.get("/audit-logs/trip/{trip_id}")
async def get_trip_audit_logs(trip_id: str, request: Request):
    """Get audit log timeline for a specific trip"""
    user = await get_admin_user(request)
    
    logs = await db.audit_logs.find({"trip_id": trip_id}, {"_id": 0}).sort("timestamp", 1).to_list(200)
    
    for log in logs:
        if isinstance(log.get("timestamp"), datetime):
            log["timestamp"] = log["timestamp"].isoformat()
    
    # Also get trip info - verify it belongs to this org
    org_filter = get_org_filter(user)
    trip = await db.trips.find_one({"trip_id": trip_id, **org_filter}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found in your organization")
    
    return {
        "trip_id": trip_id,
        "trip": trip,
        "timeline": logs
    }

# ======================== SMS LOG ENDPOINTS ========================

@router.get("/sms-logs")
async def get_sms_logs(request: Request, trip_id: Optional[str] = None, limit: int = 50):
    """Get SMS send logs - admin only, org-scoped"""
    user = await get_admin_user(request)
    org_filter = get_org_filter(user)
    
    query = {**org_filter}
    if trip_id:
        query["trip_id"] = trip_id
    
    logs = await db.sms_logs.find(query, {"_id": 0}).sort("sent_at", -1).to_list(limit)
    
    for log in logs:
        if isinstance(log.get("sent_at"), datetime):
            log["sent_at"] = log["sent_at"].isoformat()
    
    return logs
