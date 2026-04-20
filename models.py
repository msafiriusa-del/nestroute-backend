from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict
from datetime import datetime


class UserBase(BaseModel):
    email: EmailStr
    name: str
    phone: Optional[str] = None
    role: str = "parent"
    profile_image: Optional[str] = None

class UserCreate(BaseModel):
    email: EmailStr
    name: str
    password: str
    phone: str
    role: str = "parent"
    organization_name: Optional[str] = None
    manager_name: Optional[str] = None
    invite_code: Optional[str] = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class User(BaseModel):
    user_id: str
    email: str
    name: str
    phone: Optional[str] = None
    role: str
    profile_image: Optional[str] = None
    org_id: Optional[str] = None
    approval_status: Optional[str] = "approved"
    has_seen_onboarding: Optional[bool] = False
    created_at: datetime

class StudentBase(BaseModel):
    name: str
    age: int
    pickup_address: str
    dropoff_address: str
    pickup_lat: Optional[float] = None
    pickup_lng: Optional[float] = None
    dropoff_lat: Optional[float] = None
    dropoff_lng: Optional[float] = None
    notes: Optional[str] = None

class StudentCreate(StudentBase):
    parent_id: Optional[str] = None
    parent_email: Optional[str] = None
    parent_phone: Optional[str] = None

class Student(StudentBase):
    student_id: str
    parent_id: str
    created_at: datetime

class DriverBase(BaseModel):
    vehicle_type: str
    license_plate: str
    capacity: int = 4
    status: str = "active"

class DriverCreate(DriverBase):
    user_id: str

class Driver(DriverBase):
    driver_id: str
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    user_phone: Optional[str] = None
    created_at: datetime

class TripBase(BaseModel):
    date: str
    driver_id: str
    start_time: Optional[str] = "08:00"
    route_notes: Optional[str] = None

class TripCreate(TripBase):
    student_ids: List[str]
    pickup_times: Optional[Dict[str, str]] = None
    student_addresses: Optional[Dict[str, Dict[str, str]]] = None

class TripUpdate(BaseModel):
    date: Optional[str] = None
    driver_id: Optional[str] = None
    student_ids: Optional[List[str]] = None
    route_notes: Optional[str] = None
    start_time: Optional[str] = None
    student_addresses: Optional[Dict[str, Dict[str, str]]] = None

class Trip(BaseModel):
    trip_id: str
    date: str
    driver_id: str
    start_time: Optional[str] = "08:00"
    status: str
    route_notes: Optional[str] = None
    created_at: datetime

class TripAssignment(BaseModel):
    assignment_id: str
    trip_id: str
    student_id: str
    student_name: Optional[str] = None
    pickup_address: Optional[str] = None
    dropoff_address: Optional[str] = None
    pickup_time: Optional[str] = None
    dropoff_time: Optional[str] = None
    status: str

class TripStatusUpdate(BaseModel):
    status: str

class DriverTripResponse(BaseModel):
    action: str
    reason: Optional[str] = None

class AssignmentStatusUpdate(BaseModel):
    status: str
    driver_lat: Optional[float] = None
    driver_lng: Optional[float] = None
    proximity_override: bool = False

class NotificationCreate(BaseModel):
    user_id: str
    message: str
    type: str = "info"

class Notification(BaseModel):
    notification_id: str
    user_id: str
    message: str
    type: str
    trip_id: Optional[str] = None
    read_status: bool = False
    created_at: datetime

class PushTokenRegister(BaseModel):
    token: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: User

class SubscriptionCheckout(BaseModel):
    tier: str
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None

class RatingCreate(BaseModel):
    driver_id: str
    trip_id: str
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None

class LocationUpdate(BaseModel):
    trip_id: str
    latitude: float
    longitude: float

class LocationResponse(BaseModel):
    driver_id: str
    trip_id: str
    latitude: float
    longitude: float
    timestamp: str
    driver_name: Optional[str] = None

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None

class PhotoUpload(BaseModel):
    photo: str  # base64 encoded image

class VehicleDetails(BaseModel):
    make: str
    model: str
    year: int
    color: str
    license_plate: str

class IssueReport(BaseModel):
    category: str  # safety, app_bug, driver, trip, billing, other
    description: str
    trip_id: Optional[str] = None

class RouteOptimizeRequest(BaseModel):
    trip_id: str
    driver_lat: float
    driver_lng: float
