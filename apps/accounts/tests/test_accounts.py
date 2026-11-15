import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.mark.django_db
def test_register_login_profile_logout_flow():
    client = APIClient()

    # Register
    reg_res = client.post(
        "/api/v1/auth/register/",
        {
            "full_name": "Ada Lovelace",
            "username": "adalovelace",
            "email": "ada@example.com",
            "password": "StrongPassword123",
            "password_confirm": "StrongPassword123",
        },
        format="json",
    )
    assert reg_res.status_code == 201
    assert "access" in reg_res.data
    assert "refresh" in reg_res.data
    user = User.objects.get(username="adalovelace")
    assert user.password != "StrongPassword123"
    assert user.check_password("StrongPassword123")

    # Login by email
    login_res = client.post(
        "/api/v1/auth/login/",
        {"identifier": "ada@example.com", "password": "StrongPassword123"},
        format="json",
    )
    assert login_res.status_code == 200
    access = login_res.data["access"]
    refresh = login_res.data["refresh"]

    # Protected /me endpoint
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    me_res = client.get("/api/v1/auth/me/")
    assert me_res.status_code == 200
    assert me_res.data["username"] == "adalovelace"

    # Profile update
    prof_res = client.patch(
        "/api/v1/auth/profile/",
        {"full_name": "Ada King", "bio": "Systems Engineer"},
        format="json",
    )
    assert prof_res.status_code == 200
    assert prof_res.data["full_name"] == "Ada King"
    assert prof_res.data["bio"] == "Systems Engineer"

    # Logout blacklists refresh
    out_res = client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json")
    assert out_res.status_code == 200

    refresh_res = client.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json")
    assert refresh_res.status_code == 401


@pytest.mark.django_db
def test_deactivated_user_cannot_login():
    User.objects.create_user(
        username="inactiveuser",
        email="inactive@example.com",
        password="StrongPassword123",
        is_active=False,
    )
    client = APIClient()
    res = client.post(
        "/api/v1/auth/login/",
        {"identifier": "inactiveuser", "password": "StrongPassword123"},
        format="json",
    )
    assert res.status_code == 400
