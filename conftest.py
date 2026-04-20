import pytest
import requests
import os

# Use public URL for testing
BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL', '').rstrip('/')

if not BASE_URL:
    raise ValueError("EXPO_PUBLIC_BACKEND_URL environment variable is required")

@pytest.fixture
def api_client():
    """Fresh requests session for each test (function-scoped to avoid cookie contamination)"""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session

@pytest.fixture(scope="session")
def admin_token():
    """Login as admin and return token (no shared session to avoid cookies)"""
    session = requests.Session()
    response = session.post(f"{BASE_URL}/api/auth/login", json={
        "email": "admin@academy.com",
        "password": "admin123"
    })
    if response.status_code != 200:
        pytest.skip(f"Admin login failed: {response.text}")
    return response.json()["access_token"]

@pytest.fixture(scope="session")
def driver_token():
    """Login as driver and return token (no shared session to avoid cookies)"""
    session = requests.Session()
    response = session.post(f"{BASE_URL}/api/auth/login", json={
        "email": "driver@academy.com",
        "password": "driver123"
    })
    if response.status_code != 200:
        pytest.skip(f"Driver login failed: {response.text}")
    return response.json()["access_token"]

@pytest.fixture(scope="session")
def parent_token():
    """Login as parent and return token (no shared session to avoid cookies)"""
    session = requests.Session()
    response = session.post(f"{BASE_URL}/api/auth/login", json={
        "email": "parent@academy.com",
        "password": "parent123"
    })
    if response.status_code != 200:
        pytest.skip(f"Parent login failed: {response.text}")
    return response.json()["access_token"]
