"""Comprehensive tests for Academy Transport Manager API

Tests:
- Auth: login, register, token persistence, role-based access
- Admin: dashboard stats, students, drivers, trips CRUD
- Driver: trip status updates, assignment updates
- Parent: notifications, student access
"""

import pytest
import requests
import os
from datetime import datetime

BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL', '').rstrip('/')

class TestHealthAndSeed:
    """Health check and database seeding"""
    
    def test_health_check(self, api_client):
        """Test API health endpoint"""
        response = api_client.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        print("✓ Health check passed")
    
    def test_seed_database(self, api_client):
        """Seed database with demo data"""
        response = api_client.post(f"{BASE_URL}/api/seed")
        assert response.status_code == 200
        data = response.json()
        assert "credentials" in data
        assert "admin" in data["credentials"]
        assert "driver" in data["credentials"]
        assert "parent" in data["credentials"]
        print("✓ Database seeded successfully")


class TestAuthentication:
    """Authentication flow tests"""
    
    def test_admin_login(self, api_client):
        """Test admin login with correct credentials"""
        response = api_client.post(f"{BASE_URL}/api/auth/login", json={
            "email": "admin@academy.com",
            "password": "admin123"
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "user" in data
        assert data["user"]["role"] == "admin"
        assert data["user"]["email"] == "admin@academy.com"
        print("✓ Admin login successful")
    
    def test_driver_login(self, api_client):
        """Test driver login with correct credentials"""
        response = api_client.post(f"{BASE_URL}/api/auth/login", json={
            "email": "driver@academy.com",
            "password": "driver123"
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["user"]["role"] == "driver"
        print("✓ Driver login successful")
    
    def test_parent_login(self, api_client):
        """Test parent login with correct credentials"""
        response = api_client.post(f"{BASE_URL}/api/auth/login", json={
            "email": "parent@academy.com",
            "password": "parent123"
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["user"]["role"] == "parent"
        print("✓ Parent login successful")
    
    def test_invalid_login(self, api_client):
        """Test login with invalid credentials"""
        response = api_client.post(f"{BASE_URL}/api/auth/login", json={
            "email": "admin@academy.com",
            "password": "wrongpassword"
        })
        assert response.status_code == 401
        print("✓ Invalid login rejected correctly")
    
    def test_token_verification(self, api_client, admin_token):
        """Test token verification via /auth/me"""
        response = api_client.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "admin"
        assert "user_id" in data
        print("✓ Token verification successful")
    
    def test_register_new_user(self, api_client):
        """Test user registration"""
        import time
        email = f"test_user_{int(time.time())}@example.com"
        response = api_client.post(f"{BASE_URL}/api/auth/register", json={
            "email": email,
            "name": "Test User",
            "password": "testpass123",
            "role": "parent"
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["user"]["email"] == email
        assert data["user"]["role"] == "parent"
        print(f"✓ User registration successful: {email}")


class TestAdminDashboard:
    """Admin dashboard and stats tests"""
    
    def test_dashboard_stats(self, api_client, admin_token):
        """Test admin dashboard stats endpoint"""
        response = api_client.get(
            f"{BASE_URL}/api/dashboard/stats",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "total_students" in data
        assert "total_drivers" in data
        assert "total_parents" in data
        assert "today_trips" in data
        assert isinstance(data["total_students"], int)
        assert isinstance(data["total_drivers"], int)
        print(f"✓ Dashboard stats: {data['total_students']} students, {data['total_drivers']} drivers")
    
    def test_get_all_students(self, api_client, admin_token):
        """Test admin can get all students"""
        response = api_client.get(
            f"{BASE_URL}/api/students",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        students = response.json()
        assert isinstance(students, list)
        assert len(students) >= 2  # Seed creates 2 students
        if students:
            assert "student_id" in students[0]
            assert "name" in students[0]
            assert "parent_id" in students[0]
        print(f"✓ Retrieved {len(students)} students")
    
    def test_get_all_drivers(self, api_client, admin_token):
        """Test admin can get all drivers"""
        response = api_client.get(
            f"{BASE_URL}/api/drivers",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        drivers = response.json()
        assert isinstance(drivers, list)
        assert len(drivers) >= 1  # Seed creates 1 driver
        if drivers:
            assert "driver_id" in drivers[0]
            assert "user_id" in drivers[0]
            assert "vehicle_type" in drivers[0]
        print(f"✓ Retrieved {len(drivers)} drivers")


class TestTripManagement:
    """Trip creation and management tests"""
    
    def test_create_trip_as_admin(self, api_client, admin_token):
        """Test admin can create a trip"""
        # First get students and driver
        students_resp = api_client.get(
            f"{BASE_URL}/api/students",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        students = students_resp.json()
        
        drivers_resp = api_client.get(
            f"{BASE_URL}/api/drivers",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        drivers = drivers_resp.json()
        
        if not students or not drivers:
            pytest.skip("No students or drivers available")
        
        today = datetime.now().strftime("%Y-%m-%d")
        response = api_client.post(
            f"{BASE_URL}/api/trips",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "date": today,
                "driver_id": drivers[0]["driver_id"],
                "student_ids": [students[0]["student_id"]],
                "route_notes": "Test trip"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "trip_id" in data
        assert data["status"] == "scheduled"
        assert data["date"] == today
        print(f"✓ Trip created: {data['trip_id']}")
        
        # Store trip_id for later tests
        return data["trip_id"]
    
    def test_get_trips_with_enriched_data(self, api_client, admin_token):
        """Test GET /api/trips returns enriched trip data with assignments"""
        response = api_client.get(
            f"{BASE_URL}/api/trips",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        trips = response.json()
        assert isinstance(trips, list)
        
        if trips:
            trip = trips[0]
            assert "trip_id" in trip
            assert "driver_name" in trip
            assert "assignments" in trip
            assert isinstance(trip["assignments"], list)
            
            if trip["assignments"]:
                assignment = trip["assignments"][0]
                assert "student_name" in assignment
                assert "pickup_address" in assignment
                assert "status" in assignment
            print(f"✓ Retrieved {len(trips)} trips with enriched data")


class TestDriverOperations:
    """Driver-specific operations tests"""
    
    def test_driver_get_trips(self, api_client, driver_token):
        """Test driver can get their assigned trips"""
        today = datetime.now().strftime("%Y-%m-%d")
        response = api_client.get(
            f"{BASE_URL}/api/trips?date={today}",
            headers={"Authorization": f"Bearer {driver_token}"}
        )
        assert response.status_code == 200
        trips = response.json()
        assert isinstance(trips, list)
        print(f"✓ Driver retrieved {len(trips)} trips")
    
    def test_driver_start_trip(self, api_client, driver_token, admin_token):
        """Test driver can start a trip (update status to in_progress)"""
        # First create a trip as admin
        students_resp = api_client.get(
            f"{BASE_URL}/api/students",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        students = students_resp.json()
        
        drivers_resp = api_client.get(
            f"{BASE_URL}/api/drivers",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        drivers = drivers_resp.json()
        
        if not students or not drivers:
            pytest.skip("No students or drivers available")
        
        today = datetime.now().strftime("%Y-%m-%d")
        create_resp = api_client.post(
            f"{BASE_URL}/api/trips",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "date": today,
                "driver_id": drivers[0]["driver_id"],
                "student_ids": [students[0]["student_id"]],
                "route_notes": "Test trip for status update"
            }
        )
        trip_id = create_resp.json()["trip_id"]
        
        # Now update status as driver
        response = api_client.put(
            f"{BASE_URL}/api/trips/{trip_id}/status",
            headers={"Authorization": f"Bearer {driver_token}"},
            json={"status": "in_progress"}
        )
        assert response.status_code == 200
        print(f"✓ Driver started trip: {trip_id}")
        
        # Verify status changed
        verify_resp = api_client.get(
            f"{BASE_URL}/api/trips/{trip_id}",
            headers={"Authorization": f"Bearer {driver_token}"}
        )
        assert verify_resp.status_code == 200
        assert verify_resp.json()["status"] == "in_progress"
        print("✓ Trip status verified as in_progress")
    
    def test_driver_update_assignment_status(self, api_client, driver_token, admin_token):
        """Test driver can update assignment status (picked_up, dropped_off)"""
        # Create a trip with assignments
        students_resp = api_client.get(
            f"{BASE_URL}/api/students",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        students = students_resp.json()
        
        drivers_resp = api_client.get(
            f"{BASE_URL}/api/drivers",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        drivers = drivers_resp.json()
        
        if not students or not drivers:
            pytest.skip("No students or drivers available")
        
        today = datetime.now().strftime("%Y-%m-%d")
        create_resp = api_client.post(
            f"{BASE_URL}/api/trips",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "date": today,
                "driver_id": drivers[0]["driver_id"],
                "student_ids": [students[0]["student_id"]],
            }
        )
        trip_data = create_resp.json()
        trip_id = trip_data["trip_id"]
        
        # Get trip to find assignment_id
        trip_resp = api_client.get(
            f"{BASE_URL}/api/trips/{trip_id}",
            headers={"Authorization": f"Bearer {driver_token}"}
        )
        trip = trip_resp.json()
        assignment_id = trip["assignments"][0]["assignment_id"]
        
        # Update to picked_up
        response = api_client.put(
            f"{BASE_URL}/api/trips/{trip_id}/assignments/{assignment_id}/status",
            headers={"Authorization": f"Bearer {driver_token}"},
            json={"status": "picked_up"}
        )
        assert response.status_code == 200
        print(f"✓ Assignment marked as picked_up")
        
        # Update to dropped_off
        response2 = api_client.put(
            f"{BASE_URL}/api/trips/{trip_id}/assignments/{assignment_id}/status",
            headers={"Authorization": f"Bearer {driver_token}"},
            json={"status": "dropped_off"}
        )
        assert response2.status_code == 200
        print(f"✓ Assignment marked as dropped_off")


class TestParentOperations:
    """Parent-specific operations tests"""
    
    def test_parent_get_students(self, api_client, parent_token):
        """Test parent can get their children"""
        response = api_client.get(
            f"{BASE_URL}/api/students",
            headers={"Authorization": f"Bearer {parent_token}"}
        )
        assert response.status_code == 200
        students = response.json()
        assert isinstance(students, list)
        # Parent should only see their own children
        print(f"✓ Parent retrieved {len(students)} children")
    
    def test_parent_get_notifications(self, api_client, parent_token):
        """Test parent can get notifications"""
        response = api_client.get(
            f"{BASE_URL}/api/notifications",
            headers={"Authorization": f"Bearer {parent_token}"}
        )
        assert response.status_code == 200
        notifications = response.json()
        assert isinstance(notifications, list)
        print(f"✓ Parent retrieved {len(notifications)} notifications")


class TestRoleBasedAccess:
    """Role-based access control tests"""
    
    def test_driver_cannot_access_dashboard_stats(self, api_client, driver_token):
        """Test driver cannot access admin-only dashboard stats"""
        response = api_client.get(
            f"{BASE_URL}/api/dashboard/stats",
            headers={"Authorization": f"Bearer {driver_token}"}
        )
        assert response.status_code == 403
        print("✓ Driver correctly denied access to dashboard stats")
    
    def test_parent_cannot_create_driver(self, api_client, parent_token):
        """Test parent cannot create drivers (admin only)"""
        response = api_client.post(
            f"{BASE_URL}/api/drivers",
            headers={"Authorization": f"Bearer {parent_token}"},
            json={
                "user_id": "test_user_id",
                "vehicle_type": "Car",
                "license_plate": "TEST-123",
                "capacity": 4
            }
        )
        assert response.status_code == 403
        print("✓ Parent correctly denied access to create driver")
    
    def test_unauthenticated_access_denied(self, api_client):
        """Test unauthenticated requests are denied"""
        response = api_client.get(f"{BASE_URL}/api/students")
        assert response.status_code == 401
        print("✓ Unauthenticated request correctly denied")
