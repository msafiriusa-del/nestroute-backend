from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *

router = APIRouter()

@router.post("/trips", response_model=Trip)
async def create_trip(trip_data: TripCreate, request: Request):
    user = await get_admin_user(request)
    
    # Verify driver exists
    driver = await db.drivers.find_one({"driver_id": trip_data.driver_id}, {"_id": 0})
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    trip_id = f"trip_{uuid.uuid4().hex[:12]}"
    trip_doc = {
        "trip_id": trip_id,
        "date": trip_data.date,
        "driver_id": trip_data.driver_id,
        "start_time": trip_data.start_time or "08:00",
        "status": "pending_acceptance",
        "route_notes": trip_data.route_notes,
        "org_id": user.org_id,
        "created_at": datetime.now(timezone.utc)
    }
    await db.trips.insert_one(trip_doc)
    
    # Create assignments for each student
    for student_id in trip_data.student_ids:
        student = await db.students.find_one({"student_id": student_id}, {"_id": 0})
        if student:
            pickup_time = trip_data.pickup_times.get(student_id) if trip_data.pickup_times else None
            # Use override addresses if provided, else fall back to student profile
            addr_overrides = trip_data.student_addresses.get(student_id, {}) if trip_data.student_addresses else {}
            assignment = {
                "assignment_id": f"assign_{uuid.uuid4().hex[:12]}",
                "trip_id": trip_id,
                "student_id": student_id,
                "pickup_time": pickup_time,
                "dropoff_time": None,
                "pickup_address": addr_overrides.get("pickup_address") or student.get("pickup_address", ""),
                "dropoff_address": addr_overrides.get("dropoff_address") or student.get("dropoff_address", ""),
                "status": "pending"
            }
            await db.trip_assignments.insert_one(assignment)
    
    # Notify the assigned driver
    driver_user = await db.users.find_one({"user_id": driver["user_id"]}, {"_id": 0})
    if driver_user:
        await create_notification(
            driver_user["user_id"],
            f"You have been assigned a new trip for {trip_data.date} with {len(trip_data.student_ids)} student(s).",
            "trip_update",
            trip_id
        )
    
    # Notify parents
    await notify_parents_of_students(
        trip_data.student_ids,
        f"A trip has been scheduled for {trip_data.date}. Your child will be picked up.",
        "trip_update",
        trip_id
    )
    
    # Audit log for trip creation
    await create_audit_log(trip_id, "trip_created", user.user_id, {
        "date": trip_data.date,
        "driver_id": trip_data.driver_id,
        "student_count": len(trip_data.student_ids),
        "start_time": trip_data.start_time or "08:00"
    })
    
    return Trip(**trip_doc)

@router.get("/trips", response_model=List[dict])
async def get_trips(request: Request, date: Optional[str] = None, status: Optional[str] = None, sort_by: str = "priority"):
    """Get trips. sort_by: 'priority' (default - active first, by start_time), 'date_asc', 'date_desc', 'time_asc', 'time_desc'"""
    user = await get_current_user(request)
    org_filter = get_org_filter(user)
    
    query = {**org_filter}
    if date:
        query["date"] = date
    if status:
        query["status"] = status
    
    if user.role == "driver":
        driver = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
        if driver:
            query["driver_id"] = driver["driver_id"]
        else:
            return []
    elif user.role == "parent":
        # Get student IDs for this parent
        students = await db.students.find({"parent_id": user.user_id}, {"_id": 0}).to_list(100)
        student_ids = [s["student_id"] for s in students]
        
        if not student_ids:
            return []
        
        # Get trips with these students
        assignments = await db.trip_assignments.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(1000)
        trip_ids = list(set([a["trip_id"] for a in assignments]))
        query["trip_id"] = {"$in": trip_ids}
    
    trips = await db.trips.find(query, {"_id": 0}).to_list(100)
    
    if not trips:
        return []
    
    # Batch fetch all related data to avoid N+1 queries
    driver_ids = list(set(t["driver_id"] for t in trips))
    trip_ids = [t["trip_id"] for t in trips]
    
    # Fetch all drivers, users, assignments, and students in bulk
    all_drivers = await db.drivers.find({"driver_id": {"$in": driver_ids}}, {"_id": 0}).to_list(100)
    driver_lookup = {d["driver_id"]: d for d in all_drivers}
    
    driver_user_ids = [d["user_id"] for d in all_drivers]
    all_driver_users = await db.users.find({"user_id": {"$in": driver_user_ids}}, {"_id": 0}).to_list(100)
    user_lookup = {u["user_id"]: u for u in all_driver_users}
    
    all_assignments = await db.trip_assignments.find({"trip_id": {"$in": trip_ids}}, {"_id": 0}).to_list(1000)
    
    student_ids = list(set(a["student_id"] for a in all_assignments))
    all_students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(1000) if student_ids else []
    student_lookup = {s["student_id"]: s for s in all_students}
    
    # Enrich in memory
    result = []
    for trip in trips:
        drv = driver_lookup.get(trip["driver_id"])
        drv_user = user_lookup.get(drv["user_id"]) if drv else None
        
        trip_assignments = [a for a in all_assignments if a["trip_id"] == trip["trip_id"]]
        enriched_assignments = []
        for a in trip_assignments:
            st = student_lookup.get(a["student_id"])
            enriched_assignments.append({
                **a,
                "student_name": st.get("name") if st else None,
                "pickup_address": a.get("pickup_address") or (st.get("pickup_address") if st else None),
                "dropoff_address": a.get("dropoff_address") or (st.get("dropoff_address") if st else None),
            })
        
        result.append({
            **trip,
            "driver_name": drv_user.get("name") if drv_user else None,
            "driver_phone": drv_user.get("phone") if drv_user else None,
            "driver_photo": drv_user.get("profile_image") if drv_user else None,
            "driver_vehicle": drv.get("vehicle_type") if drv else None,
            "driver_license_plate": drv.get("license_plate") if drv else None,
            "driver_capacity": drv.get("capacity") if drv else None,
            "assignments": enriched_assignments,
            "created_at": trip["created_at"].isoformat() if isinstance(trip["created_at"], datetime) else trip["created_at"]
        })
    
    # Sort results
    STATUS_PRIORITY = {
        "in_progress": 0,
        "pending_acceptance": 1,
        "scheduled": 2,
        "completed": 3,
        "declined": 4,
    }
    
    def sort_key(t):
        time_str = t.get("start_time", "23:59")
        date_str = t.get("date", "9999-99-99")
        status_rank = STATUS_PRIORITY.get(t.get("status", ""), 5)
        return (status_rank, date_str, time_str)
    
    def sort_key_date_asc(t):
        return (t.get("date", "9999-99-99"), t.get("start_time", "23:59"))
    
    def sort_key_date_desc(t):
        return (t.get("date", "0000-00-00"), t.get("start_time", "00:00"))
    
    def sort_key_time(t):
        return (t.get("start_time", "23:59"), t.get("date", "9999-99-99"))
    
    if sort_by == "date_asc":
        result.sort(key=sort_key_date_asc)
    elif sort_by == "date_desc":
        result.sort(key=sort_key_date_desc, reverse=True)
    elif sort_by == "time_asc":
        result.sort(key=sort_key_time)
    elif sort_by == "time_desc":
        result.sort(key=sort_key_time, reverse=True)
    else:  # "priority" - default
        result.sort(key=sort_key)
    
    return result

@router.get("/trips/{trip_id}")
async def get_trip(trip_id: str, request: Request):
    user = await get_current_user(request)
    
    trip = await db.trips.find_one({"trip_id": trip_id}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    driver = await db.drivers.find_one({"driver_id": trip["driver_id"]}, {"_id": 0})
    driver_user = None
    if driver:
        driver_user = await db.users.find_one({"user_id": driver["user_id"]}, {"_id": 0})
    
    assignments = await db.trip_assignments.find({"trip_id": trip_id}, {"_id": 0}).to_list(100)
    
    # Batch fetch students for assignments
    student_ids = list(set(a["student_id"] for a in assignments))
    all_students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(100) if student_ids else []
    student_lookup = {s["student_id"]: s for s in all_students}
    
    enriched_assignments = []
    for a in assignments:
        st = student_lookup.get(a["student_id"])
        enriched_assignments.append({
            **a,
            "student_name": st.get("name") if st else None,
            "pickup_address": a.get("pickup_address") or (st.get("pickup_address") if st else None),
            "dropoff_address": a.get("dropoff_address") or (st.get("dropoff_address") if st else None),
        })
    
    return {
        **trip,
        "driver_name": driver_user.get("name") if driver_user else None,
        "driver_phone": driver_user.get("phone") if driver_user else None,
        "driver_photo": driver_user.get("profile_image") if driver_user else None,
        "driver_vehicle": driver.get("vehicle_type") if driver else None,
        "driver_license_plate": driver.get("license_plate") if driver else None,
        "driver_capacity": driver.get("capacity") if driver else None,
        "assignments": enriched_assignments,
        "created_at": trip["created_at"].isoformat() if isinstance(trip["created_at"], datetime) else trip["created_at"]
    }

@router.put("/trips/{trip_id}/driver-response")
async def driver_respond_to_trip(trip_id: str, response_data: DriverTripResponse, request: Request):
    """Driver accepts or declines a trip assignment"""
    user = await get_driver_user(request)
    
    trip = await db.trips.find_one({"trip_id": trip_id}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    if trip["status"] != "pending_acceptance":
        raise HTTPException(status_code=400, detail="Trip is not pending acceptance")
    
    # Verify driver owns this trip
    if user.role == "driver":
        driver = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
        if not driver or driver["driver_id"] != trip["driver_id"]:
            raise HTTPException(status_code=403, detail="Not your trip")
    
    if response_data.action == "accept":
        await db.trips.update_one(
            {"trip_id": trip_id},
            {"$set": {"status": "scheduled"}}
        )
        # Notify all admins
        admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(100)
        for admin in admins:
            await create_notification(
                admin["user_id"],
                f"Driver {user.name} accepted the trip on {trip['date']}.",
                "trip_update",
                trip_id
            )
        return {"message": "Trip accepted", "status": "scheduled"}
    
    elif response_data.action == "decline":
        await db.trips.update_one(
            {"trip_id": trip_id},
            {"$set": {"status": "declined"}}
        )
        # Notify all admins
        admins = await db.users.find({"role": "admin"}, {"_id": 0}).to_list(100)
        decline_msg = f"Driver {user.name} declined the trip on {trip['date']}."
        if response_data.reason:
            decline_msg += f" Reason: {response_data.reason}"
        for admin in admins:
            await create_notification(
                admin["user_id"],
                decline_msg,
                "alert",
                trip_id
            )
        return {"message": "Trip declined", "status": "declined"}
    
    raise HTTPException(status_code=400, detail="Invalid action. Use 'accept' or 'decline'")

@router.put("/trips/{trip_id}/status")
async def update_trip_status(trip_id: str, status_update: TripStatusUpdate, request: Request):
    user = await get_driver_user(request)
    
    trip = await db.trips.find_one({"trip_id": trip_id}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    # Verify driver owns this trip (unless admin)
    if user.role == "driver":
        driver = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
        if not driver or driver["driver_id"] != trip["driver_id"]:
            raise HTTPException(status_code=403, detail="Not your trip")
    
    # ===== START TIME RESTRICTION =====
    # Can only start trip within 2 hours of scheduled start_time
    if status_update.status == "in_progress":
        trip_start_time = trip.get("start_time", "08:00")
        try:
            trip_datetime = datetime.strptime(f"{trip['date']} {trip_start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            time_until_trip = (trip_datetime - now).total_seconds()
            # Block if more than 2 hours (7200 seconds) before trip
            if time_until_trip > 7200:
                hours_left = int(time_until_trip // 3600)
                mins_left = int((time_until_trip % 3600) // 60)
                # Convert to 12hr format for user-facing message
                h, m = map(int, trip_start_time.split(':'))
                suffix = 'PM' if h >= 12 else 'AM'
                h12 = h if h == 12 else (h % 12 or 12)
                time_12 = f"{h12}:{m:02d} {suffix}"
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot start trip yet. Trip starts at {time_12} on {trip['date']}. You can start 2 hours before ({hours_left}h {mins_left}m remaining)."
                )
        except ValueError:
            pass  # If date/time parsing fails, allow start
    # ===== END START TIME RESTRICTION =====
    
    await db.trips.update_one(
        {"trip_id": trip_id},
        {"$set": {"status": status_update.status}}
    )
    
    # Get all student IDs for this trip
    assignments = await db.trip_assignments.find({"trip_id": trip_id}, {"_id": 0}).to_list(100)
    student_ids = [a["student_id"] for a in assignments]
    
    # Notify parents
    if status_update.status == "in_progress":
        await notify_parents_of_students(
            student_ids,
            "Driver has started the trip. Your child will be picked up soon.",
            "trip_update",
            trip_id
        )
        # SMS notification
        await sms_notify_parents(
            student_ids,
            "NestRoute: Driver has started the trip. Your child will be picked up soon.",
            trip_id,
            "trip_started"
        )
        # Audit log
        await create_audit_log(trip_id, "trip_started", user.user_id, {"status": "in_progress"})
    elif status_update.status == "completed":
        await notify_parents_of_students(
            student_ids,
            "Trip completed. All children have been dropped off.",
            "trip_update",
            trip_id
        )
        # SMS notification
        await sms_notify_parents(
            student_ids,
            "NestRoute: Trip completed. All children have been safely dropped off.",
            trip_id,
            "trip_completed"
        )
        # Audit log
        await create_audit_log(trip_id, "trip_completed", user.user_id, {"status": "completed"})
    
    # Broadcast via WebSocket
    students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(100)
    parent_ids = list(set([s["parent_id"] for s in students]))
    
    await manager.broadcast_to_users({
        "type": "trip_status_update",
        "data": {
            "trip_id": trip_id,
            "status": status_update.status
        }
    }, parent_ids)
    
    return {"message": "Trip status updated"}

@router.put("/trips/{trip_id}/assignments/{assignment_id}/status")
async def update_assignment_status(trip_id: str, assignment_id: str, status_update: AssignmentStatusUpdate, request: Request):
    user = await get_driver_user(request)
    
    trip = await db.trips.find_one({"trip_id": trip_id}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    # Verify driver owns this trip
    if user.role == "driver":
        driver = await db.drivers.find_one({"user_id": user.user_id}, {"_id": 0})
        if not driver or driver["driver_id"] != trip["driver_id"]:
            raise HTTPException(status_code=403, detail="Not your trip")
    
    assignment = await db.trip_assignments.find_one({"assignment_id": assignment_id}, {"_id": 0})
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # ===== LOGIC ENFORCEMENT =====
    # Prevent pickup unless trip is in_progress
    if status_update.status == "picked_up" and trip["status"] != "in_progress":
        raise HTTPException(status_code=400, detail="Cannot pick up student: trip has not started yet")
    # Prevent drop-off unless student is picked_up
    if status_update.status == "dropped_off" and assignment["status"] != "picked_up":
        raise HTTPException(status_code=400, detail="Cannot drop off student: student has not been picked up yet")
    # Prevent going backwards
    if status_update.status == "pending":
        raise HTTPException(status_code=400, detail="Cannot revert assignment to pending")
    
    # ===== PROXIMITY ENFORCEMENT =====
    student = await db.students.find_one({"student_id": assignment["student_id"]}, {"_id": 0})
    if student and status_update.driver_lat is not None and status_update.driver_lng is not None:
        target_lat = None
        target_lng = None
        location_type = ""
        if status_update.status == "picked_up":
            target_lat = student.get("pickup_lat")
            target_lng = student.get("pickup_lng")
            location_type = "pickup"
        elif status_update.status == "dropped_off":
            target_lat = student.get("dropoff_lat")
            target_lng = student.get("dropoff_lng")
            location_type = "dropoff"
        
        if target_lat and target_lng:
            prox = check_proximity(status_update.driver_lat, status_update.driver_lng, target_lat, target_lng)
            if not prox["within_threshold"] and not status_update.proximity_override:
                raise HTTPException(
                    status_code=400,
                    detail=f"You are {int(prox['distance_meters'])}m away from the {location_type} location. "
                           f"You must be within {PROXIMITY_THRESHOLD_METERS}m to proceed. "
                           f"If you need to override, confirm the proximity override."
                )
    elif student and (status_update.driver_lat is None or status_update.driver_lng is None):
        # Driver didn't send GPS - require it
        raise HTTPException(
            status_code=400,
            detail="GPS location required. Please enable location services and try again."
        )
    # ===== END ENFORCEMENT =====
    
    update_data = {"status": status_update.status}
    now = datetime.now(timezone.utc)
    if status_update.status == "picked_up":
        update_data["pickup_time"] = now.strftime("%H:%M")
        update_data["actual_pickup_time"] = now
        # Calculate delay vs scheduled
        scheduled = assignment.get("scheduled_pickup_time") or assignment.get("pickup_time")
        if scheduled and isinstance(scheduled, str) and ":" in scheduled:
            try:
                trip_date = trip.get("date", now.strftime("%Y-%m-%d"))
                sched_dt = datetime.strptime(f"{trip_date} {scheduled}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                delay_min = int((now - sched_dt).total_seconds() / 60)
                update_data["pickup_delay_minutes"] = max(0, delay_min)
            except ValueError:
                pass
    elif status_update.status == "dropped_off":
        update_data["dropoff_time"] = now.strftime("%H:%M")
        update_data["actual_dropoff_time"] = now
    
    await db.trip_assignments.update_one(
        {"assignment_id": assignment_id},
        {"$set": update_data}
    )
    
    # Get student and parent
    student = await db.students.find_one({"student_id": assignment["student_id"]}, {"_id": 0})
    if student:
        parent_id = student["parent_id"]
        
        # Format time in 12-hour format
        time_str = now.strftime("%-I:%M %p") if hasattr(now, 'strftime') else now.strftime("%I:%M %p").lstrip('0')
        
        if status_update.status == "picked_up":
            delay_info = ""
            delay_min = update_data.get("pickup_delay_minutes", 0)
            if delay_min > 5:
                delay_info = f" ({delay_min} min late)"
            message = f"{student['name']} picked up at {time_str}{delay_info}"
            sms_message = f"NestRoute: {student['name']} has been picked up at {time_str}.{' ' + delay_info.strip() if delay_info else ''}"
        else:
            message = f"{student['name']} dropped off at {time_str}"
            sms_message = f"NestRoute: {student['name']} has been safely dropped off at {time_str}."
        
        await create_notification(parent_id, message, "trip_update", trip_id)
        
        # SMS notification to parent
        parent = await db.users.find_one({"user_id": parent_id}, {"_id": 0})
        if parent and parent.get("phone"):
            # Check SMS subscription
            admin_sub = await db.subscriptions.find_one({"status": "active", "sms_enabled": True})
            if admin_sub:
                await send_sms(parent["phone"], sms_message, trip_id, f"{status_update.status}_{assignment['student_id']}")
        
        # Audit log with proximity data
        proximity_info = {}
        if status_update.driver_lat is not None and status_update.driver_lng is not None:
            target_lat = None
            target_lng = None
            if status_update.status == "picked_up":
                target_lat = student.get("pickup_lat")
                target_lng = student.get("pickup_lng")
            elif status_update.status == "dropped_off":
                target_lat = student.get("dropoff_lat")
                target_lng = student.get("dropoff_lng")
            
            if target_lat and target_lng:
                prox = check_proximity(status_update.driver_lat, status_update.driver_lng, target_lat, target_lng)
                proximity_info = {
                    "distance_meters": prox["distance_meters"],
                    "within_threshold": prox["within_threshold"],
                    "proximity_override": status_update.proximity_override,
                }
            proximity_info["driver_lat"] = status_update.driver_lat
            proximity_info["driver_lng"] = status_update.driver_lng
        
        await create_audit_log(trip_id, f"student_{status_update.status}", user.user_id, {
            "student_id": assignment["student_id"],
            "student_name": student["name"],
            "assignment_id": assignment_id,
            "actual_time": now.isoformat(),
            "delay_minutes": update_data.get("pickup_delay_minutes"),
            **proximity_info
        })
        
        # WebSocket update
        await manager.send_personal_message({
            "type": "assignment_status_update",
            "data": {
                "trip_id": trip_id,
                "assignment_id": assignment_id,
                "student_id": assignment["student_id"],
                "student_name": student["name"],
                "status": status_update.status,
                "actual_time": now.isoformat(),
                "formatted_time": time_str,
            }
        }, parent_id)
    
    # Check if all assignments are dropped off
    all_assignments = await db.trip_assignments.find({"trip_id": trip_id}, {"_id": 0}).to_list(100)
    all_dropped = all(a["status"] == "dropped_off" for a in all_assignments if a["assignment_id"] != assignment_id)
    if all_dropped and status_update.status == "dropped_off":
        await db.trips.update_one(
            {"trip_id": trip_id},
            {"$set": {"status": "completed"}}
        )
    
    return {"message": "Assignment status updated"}

@router.put("/trips/{trip_id}")
async def update_trip(trip_id: str, trip_update: TripUpdate, request: Request):
    """Edit a trip — change date, driver, students, or notes. Only admin, only non-completed trips."""
    await get_admin_user(request)
    
    trip = await db.trips.find_one({"trip_id": trip_id}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    if trip["status"] == "completed":
        raise HTTPException(status_code=400, detail="Cannot edit a completed trip")
    
    # Build update dict for trip fields
    update_fields = {}
    if trip_update.date is not None:
        update_fields["date"] = trip_update.date
    if trip_update.driver_id is not None:
        driver = await db.drivers.find_one({"driver_id": trip_update.driver_id}, {"_id": 0})
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")
        update_fields["driver_id"] = trip_update.driver_id
        # Notify the new driver
        driver_user = await db.users.find_one({"user_id": driver["user_id"]}, {"_id": 0})
        if driver_user and trip_update.driver_id != trip["driver_id"]:
            await create_notification(
                driver_user["user_id"],
                f"You have been reassigned to a trip on {trip_update.date or trip['date']}.",
                "trip_update",
                trip_id
            )
    if trip_update.route_notes is not None:
        update_fields["route_notes"] = trip_update.route_notes
    
    if update_fields:
        await db.trips.update_one({"trip_id": trip_id}, {"$set": update_fields})
    
    # Handle student changes
    if trip_update.student_ids is not None:
        current_assignments = await db.trip_assignments.find({"trip_id": trip_id}, {"_id": 0}).to_list(100)
        current_student_ids = set(a["student_id"] for a in current_assignments)
        new_student_ids = set(trip_update.student_ids)
        
        # Remove students no longer in the trip
        to_remove = current_student_ids - new_student_ids
        if to_remove:
            await db.trip_assignments.delete_many({"trip_id": trip_id, "student_id": {"$in": list(to_remove)}})
        
        # Add new students
        to_add = new_student_ids - current_student_ids
        for student_id in to_add:
            student = await db.students.find_one({"student_id": student_id}, {"_id": 0})
            if student:
                await db.trip_assignments.insert_one({
                    "assignment_id": f"assign_{uuid.uuid4().hex[:12]}",
                    "trip_id": trip_id,
                    "student_id": student_id,
                    "pickup_time": None,
                    "dropoff_time": None,
                    "status": "pending"
                })
        
        # Notify parents of newly added students
        if to_add:
            trip_date = trip_update.date or trip["date"]
            await notify_parents_of_students(
                list(to_add),
                f"Your child has been added to a trip on {trip_date}.",
                "trip_update",
                trip_id
            )
    
    return {"message": "Trip updated successfully"}

@router.delete("/trips/{trip_id}")
async def delete_trip(trip_id: str, request: Request):
    await get_admin_user(request)
    
    result = await db.trips.delete_one({"trip_id": trip_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    await db.trip_assignments.delete_many({"trip_id": trip_id})
    
    return {"message": "Trip deleted"}



@router.post("/trips/{trip_id}/optimize-route")
async def optimize_trip_route(trip_id: str, route_req: RouteOptimizeRequest, request: Request):
    """Optimize pickup/dropoff route for a trip using nearest-neighbor algorithm."""
    user = await get_driver_user(request)
    
    trip = await db.trips.find_one({"trip_id": trip_id}, {"_id": 0})
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    assignments = await db.trip_assignments.find({"trip_id": trip_id}, {"_id": 0}).to_list(100)
    student_ids = [a["student_id"] for a in assignments]
    students = await db.students.find({"student_id": {"$in": student_ids}}, {"_id": 0}).to_list(100)
    student_lookup = {s["student_id"]: s for s in students}
    
    # Build stops list: pending pickups first, then pending dropoffs
    pickup_stops = []
    dropoff_stops = []
    
    for a in assignments:
        student = student_lookup.get(a["student_id"], {})
        if a["status"] == "pending" and student.get("pickup_lat") and student.get("pickup_lng"):
            pickup_stops.append({
                "assignment_id": a["assignment_id"],
                "student_id": a["student_id"],
                "student_name": student.get("name", "Unknown"),
                "type": "pickup",
                "address": student.get("pickup_address", ""),
                "lat": student["pickup_lat"],
                "lng": student["pickup_lng"],
            })
        elif a["status"] == "picked_up" and student.get("dropoff_lat") and student.get("dropoff_lng"):
            dropoff_stops.append({
                "assignment_id": a["assignment_id"],
                "student_id": a["student_id"],
                "student_name": student.get("name", "Unknown"),
                "type": "dropoff",
                "address": student.get("dropoff_address", ""),
                "lat": student["dropoff_lat"],
                "lng": student["dropoff_lng"],
            })
    
    # Optimize: pickups first (nearest neighbor), then dropoffs (nearest neighbor)
    optimized_pickups = optimize_route_nearest_neighbor(route_req.driver_lat, route_req.driver_lng, pickup_stops)
    
    # For dropoffs, start from last pickup location
    if optimized_pickups:
        last_pickup = optimized_pickups[-1]
        optimized_dropoffs = optimize_route_nearest_neighbor(last_pickup["lat"], last_pickup["lng"], dropoff_stops)
    else:
        optimized_dropoffs = optimize_route_nearest_neighbor(route_req.driver_lat, route_req.driver_lng, dropoff_stops)
    
    all_stops = optimized_pickups + optimized_dropoffs
    
    # Add distance info
    prev_lat, prev_lng = route_req.driver_lat, route_req.driver_lng
    total_distance = 0
    for stop in all_stops:
        d = haversine_distance(prev_lat, prev_lng, stop["lat"], stop["lng"])
        stop["distance_from_prev"] = round(d, 0)
        total_distance += d
        prev_lat, prev_lng = stop["lat"], stop["lng"]
    
    return {
        "trip_id": trip_id,
        "driver_location": {"lat": route_req.driver_lat, "lng": route_req.driver_lng},
        "optimized_stops": all_stops,
        "total_stops": len(all_stops),
        "total_distance_meters": round(total_distance, 0),
    }

@router.get("/trips/{trip_id}/proximity-check")
async def check_assignment_proximity(trip_id: str, request: Request, assignment_id: str = "", driver_lat: float = 0, driver_lng: float = 0):
    """Check proximity of driver to a specific assignment's location. Used by frontend before pickup/dropoff."""
    user = await get_driver_user(request)
    
    assignment = await db.trip_assignments.find_one({"assignment_id": assignment_id}, {"_id": 0})
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    student = await db.students.find_one({"student_id": assignment["student_id"]}, {"_id": 0})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Determine target based on assignment status
    if assignment["status"] == "pending":
        target_lat = student.get("pickup_lat")
        target_lng = student.get("pickup_lng")
        location_type = "pickup"
    else:
        target_lat = student.get("dropoff_lat")
        target_lng = student.get("dropoff_lng")
        location_type = "dropoff"
    
    if not target_lat or not target_lng:
        return {
            "has_coordinates": False,
            "within_threshold": True,  # Allow if no coordinates set
            "message": "No coordinates set for this location"
        }
    
    prox = check_proximity(driver_lat, driver_lng, target_lat, target_lng)
    return {
        "has_coordinates": True,
        "location_type": location_type,
        "student_name": student.get("name"),
        "address": student.get(f"{location_type}_address"),
        **prox
    }
