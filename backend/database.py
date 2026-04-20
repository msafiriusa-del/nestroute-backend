"""Shared database connection, WebSocket manager, and configuration."""
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import WebSocket
from dotenv import load_dotenv
from pathlib import Path
import os
import logging
from typing import List, Dict

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'academy_transport')]

# JWT Config
SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'academy-transport-secret-key-2025')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

# Twilio Config
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_MESSAGING_SERVICE_SID = os.environ.get('TWILIO_MESSAGING_SERVICE_SID')

twilio_client = None
try:
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        from twilio.rest import Client as TwilioClient
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("Twilio client initialized")
except Exception as e:
    print(f"Twilio init error: {e}")

# Stripe Config
import stripe
STRIPE_API_KEY = os.environ.get('STRIPE_API_KEY')
if STRIPE_API_KEY:
    stripe.api_key = STRIPE_API_KEY
    print("Stripe configured")

# Subscription Tiers
SUBSCRIPTION_TIERS = {
    "starter": {
        "name": "Starter",
        "price_monthly": 3900,
        "per_student_price": 0,
        "student_limit": 20,
        "driver_limit": 3,
        "sms_enabled": False,
        "features": ["Basic trip management", "Up to 20 students", "Up to 3 drivers", "Email & push notifications"],
    },
    "growth": {
        "name": "Growth",
        "price_monthly": 3900,
        "per_student_price": 200,
        "student_limit": 100,
        "driver_limit": 10,
        "sms_enabled": True,
        "features": ["Everything in Starter", "Up to 100 students", "Up to 10 drivers", "SMS notifications to parents", "Priority support"],
    },
    "premium": {
        "name": "Premium",
        "price_monthly": 12900,
        "per_student_price": 0,
        "student_limit": -1,
        "driver_limit": -1,
        "sms_enabled": True,
        "features": ["Everything in Growth", "Unlimited students & drivers", "SMS notifications", "Custom reporting", "Dedicated support", "API access"],
    },
}

# Logger
logger = logging.getLogger("nestroute")

# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        logger.info(f"WebSocket connected for user: {user_id}")
    
    def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
        logger.info(f"WebSocket disconnected for user: {user_id}")
    
    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.error(f"Error sending message to {user_id}: {e}")
    
    async def broadcast_to_users(self, message: dict, user_ids: List[str]):
        for user_id in user_ids:
            await self.send_personal_message(message, user_id)

manager = ConnectionManager()

# Geo constants
PROXIMITY_THRESHOLD_METERS = 100
