from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *
from fastapi import WebSocket, WebSocketDisconnect
router = APIRouter()

@router.get("/notifications", response_model=List[Notification])
async def get_notifications(request: Request, unread_only: bool = False):
    user = await get_current_user(request)
    
    query = {"user_id": user.user_id}
    if unread_only:
        query["read_status"] = False
    
    notifications = await db.notifications.find(query, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [Notification(**n) for n in notifications]

@router.put("/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, request: Request):
    user = await get_current_user(request)
    
    result = await db.notifications.update_one(
        {"notification_id": notification_id, "user_id": user.user_id},
        {"$set": {"read_status": True}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    return {"message": "Marked as read"}

@router.put("/notifications/read-all")
async def mark_all_notifications_read(request: Request):
    user = await get_current_user(request)
    
    await db.notifications.update_many(
        {"user_id": user.user_id},
        {"$set": {"read_status": True}}
    )
    
    return {"message": "All notifications marked as read"}

@router.get("/notifications/unread-count")
async def get_unread_count(request: Request):
    user = await get_current_user(request)
    count = await db.notifications.count_documents({"user_id": user.user_id, "read_status": False})
    return {"count": count}

# ======================== PUSH TOKEN ENDPOINTS ========================

@router.post("/push-tokens")
async def register_push_token(token_data: PushTokenRegister, request: Request):
    user = await get_current_user(request)
    
    await db.push_tokens.update_one(
        {"user_id": user.user_id},
        {
            "$set": {
                "token": token_data.token,
                "updated_at": datetime.now(timezone.utc)
            },
            "$setOnInsert": {
                "created_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )
    
    return {"message": "Push token registered"}

# Mounted on /api/ws so it goes through the proxy that routes /api/* to port 8001

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_text()
            # Handle ping/pong for connection keep-alive
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
