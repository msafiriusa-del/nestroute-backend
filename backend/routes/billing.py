from fastapi import APIRouter, HTTPException, Request, Response
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import uuid

from database import db, manager, logger, SECRET_KEY, ALGORITHM
from helpers import get_current_user, get_admin_user, get_driver_user, get_org_filter, create_notification, notify_parents_of_students, send_sms, sms_notify_parents, create_audit_log, get_admin_subscription, check_subscription_limits, hash_password, verify_password, create_access_token, haversine_distance, check_proximity, optimize_route_nearest_neighbor
from models import *
import stripe
from database import SUBSCRIPTION_TIERS, STRIPE_API_KEY
router = APIRouter()

@router.get("/subscription/tiers")
async def get_subscription_tiers():
    """Get available subscription tiers"""
    tiers = []
    for key, tier in SUBSCRIPTION_TIERS.items():
        tiers.append({
            "id": key,
            **tier,
            "price_display": f"${tier['price_monthly'] / 100:.0f}/mo" + (f" + ${tier['per_student_price'] / 100:.0f}/student" if tier.get('per_student_price', 0) > 0 else ""),
        })
    return tiers

@router.get("/subscription/status")
async def get_subscription_status(request: Request):
    """Get current admin's subscription status"""
    user = await get_admin_user(request)
    
    sub = await db.subscriptions.find_one(
        {"user_id": user.user_id},
        {"_id": 0}
    )
    
    if not sub:
        return {
            "status": "none",
            "tier": None,
            "message": "No active subscription. You are on the free tier.",
            "limits": {"student_limit": 5, "driver_limit": 1, "sms_enabled": False}
        }
    
    tier_info = SUBSCRIPTION_TIERS.get(sub.get("tier", "starter"), SUBSCRIPTION_TIERS["starter"])
    return {
        "status": sub.get("status", "inactive"),
        "tier": sub.get("tier"),
        "tier_name": tier_info["name"],
        "stripe_subscription_id": sub.get("stripe_subscription_id"),
        "current_period_end": sub.get("current_period_end"),
        "limits": {
            "student_limit": tier_info["student_limit"],
            "driver_limit": tier_info["driver_limit"],
            "sms_enabled": tier_info["sms_enabled"],
        },
        "features": tier_info["features"],
    }

@router.post("/subscription/checkout")
async def create_checkout_session(checkout_data: SubscriptionCheckout, request: Request):
    """Create a Stripe Checkout Session for subscription"""
    user = await get_admin_user(request)
    
    tier = SUBSCRIPTION_TIERS.get(checkout_data.tier)
    if not tier:
        raise HTTPException(status_code=400, detail="Invalid subscription tier")
    
    if not STRIPE_API_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    
    try:
        # Create or get Stripe customer
        existing_sub = await db.subscriptions.find_one({"user_id": user.user_id}, {"_id": 0})
        customer_id = existing_sub.get("stripe_customer_id") if existing_sub else None
        
        if not customer_id:
            customer = stripe.Customer.create(
                email=user.email,
                name=user.name,
                metadata={"user_id": user.user_id}
            )
            customer_id = customer.id
        
        # Create a price for this tier
        price = stripe.Price.create(
            unit_amount=tier["price_monthly"],
            currency="usd",
            recurring={"interval": "month"},
            product_data={"name": f"NestRoute - {tier['name']} Plan"},
        )
        
        line_items = [{"price": price.id, "quantity": 1}]
        
        # For growth tier, add per-student pricing
        if checkout_data.tier == "growth" and tier.get("per_student_price", 0) > 0:
            student_count = await db.students.count_documents({})
            if student_count > 0:
                student_price = stripe.Price.create(
                    unit_amount=tier["per_student_price"],
                    currency="usd",
                    recurring={"interval": "month"},
                    product_data={"name": "Per-Student Fee"},
                )
                line_items.append({"price": student_price.id, "quantity": max(student_count, 1)})
        
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=line_items,
            mode="subscription",
            success_url=checkout_data.success_url or "https://academy-transport.app/success",
            cancel_url=checkout_data.cancel_url or "https://academy-transport.app/cancel",
            metadata={
                "user_id": user.user_id,
                "tier": checkout_data.tier,
            }
        )
        
        # Save pending subscription
        await db.subscriptions.update_one(
            {"user_id": user.user_id},
            {
                "$set": {
                    "user_id": user.user_id,
                    "tier": checkout_data.tier,
                    "status": "pending",
                    "stripe_customer_id": customer_id,
                    "stripe_checkout_session_id": session.id,
                    "sms_enabled": tier["sms_enabled"],
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True
        )
        
        return {"checkout_url": session.url, "session_id": session.id}
    
    except stripe.StripeError as e:
        logger.error(f"Stripe error: {e}")
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

@router.post("/subscription/activate")
async def activate_subscription_manually(request: Request):
    """Manually activate subscription (for demo/testing without real Stripe webhooks)"""
    user = await get_admin_user(request)
    body = await request.json()
    tier = body.get("tier", "starter")
    
    if tier not in SUBSCRIPTION_TIERS:
        raise HTTPException(status_code=400, detail="Invalid tier")
    
    tier_info = SUBSCRIPTION_TIERS[tier]
    
    await db.subscriptions.update_one(
        {"user_id": user.user_id},
        {
            "$set": {
                "user_id": user.user_id,
                "tier": tier,
                "status": "active",
                "sms_enabled": tier_info["sms_enabled"],
                "org_id": user.org_id,
                "stripe_subscription_id": f"sub_demo_{uuid.uuid4().hex[:12]}",
                "current_period_end": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True
    )
    
    return {"message": f"{tier_info['name']} plan activated successfully", "tier": tier, "status": "active"}

@router.post("/subscription/cancel")
async def cancel_subscription(request: Request):
    """Cancel the current subscription"""
    user = await get_admin_user(request)
    
    sub = await db.subscriptions.find_one({"user_id": user.user_id}, {"_id": 0})
    if not sub or sub.get("status") != "active":
        raise HTTPException(status_code=400, detail="No active subscription to cancel")
    
    # If real Stripe subscription, cancel it
    if sub.get("stripe_subscription_id") and not sub["stripe_subscription_id"].startswith("sub_demo_"):
        try:
            stripe.Subscription.modify(
                sub["stripe_subscription_id"],
                cancel_at_period_end=True
            )
        except stripe.StripeError as e:
            logger.error(f"Stripe cancel error: {e}")
    
    await db.subscriptions.update_one(
        {"user_id": user.user_id},
        {"$set": {"status": "cancelled", "updated_at": datetime.now(timezone.utc)}}
    )
    
    return {"message": "Subscription cancelled"}

@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events"""
    payload = await request.body()
    
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    
    event_type = event.get("type")
    data = event.get("data", {}).get("object", {})
    
    logger.info(f"Stripe webhook: {event_type}")
    
    if event_type == "checkout.session.completed":
        user_id = data.get("metadata", {}).get("user_id")
        tier = data.get("metadata", {}).get("tier", "starter")
        subscription_id = data.get("subscription")
        customer_id = data.get("customer")
        
        if user_id:
            tier_info = SUBSCRIPTION_TIERS.get(tier, SUBSCRIPTION_TIERS["starter"])
            await db.subscriptions.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "status": "active",
                        "tier": tier,
                        "stripe_subscription_id": subscription_id,
                        "stripe_customer_id": customer_id,
                        "sms_enabled": tier_info["sms_enabled"],
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True
            )
            logger.info(f"Subscription activated for {user_id}: {tier}")
    
    elif event_type in ["customer.subscription.updated", "customer.subscription.deleted"]:
        subscription_id = data.get("id")
        status = data.get("status")
        
        if subscription_id:
            update_status = "active" if status == "active" else "cancelled"
            await db.subscriptions.update_one(
                {"stripe_subscription_id": subscription_id},
                {"$set": {"status": update_status, "updated_at": datetime.now(timezone.utc)}}
            )
    
    elif event_type == "invoice.paid":
        subscription_id = data.get("subscription")
        period_end = data.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")
        
        if subscription_id and period_end:
            await db.subscriptions.update_one(
                {"stripe_subscription_id": subscription_id},
                {"$set": {
                    "current_period_end": datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat(),
                    "status": "active",
                    "updated_at": datetime.now(timezone.utc),
                }}
            )
    
    return {"received": True}
