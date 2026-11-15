import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.mark.django_db
def test_admin_panel_server_side_authorization():
    admin = User.objects.create_user(
        username="sysadmin",
        email="sysadmin@example.com",
        password="Password123!",
        is_staff=True,
        role="admin",
    )
    member = User.objects.create_user(
        username="regular",
        email="regular@example.com",
        password="Password123!",
    )

    member_client = APIClient()
    member_client.force_authenticate(user=member)

    admin_client = APIClient()
    admin_client.force_authenticate(user=admin)

    # Regular member strictly denied
    assert member_client.get("/api/v1/admin/overview/").status_code == 403
    assert member_client.patch(f"/api/v1/admin/users/{member.id}/status/", {"is_active": False}, format="json").status_code == 403

    # Admin succeeds
    overview = admin_client.get("/api/v1/admin/overview/")
    assert overview.status_code == 200
    assert overview.data["stats"]["total_users"] == 2

    # Admin can deactivate member
    toggle = admin_client.patch(
        f"/api/v1/admin/users/{member.id}/status/",
        {"is_active": False},
        format="json",
    )
    assert toggle.status_code == 200
    member.refresh_from_db()
    assert member.is_active is False

    # Admin cannot deactivate self
    self_toggle = admin_client.patch(
        f"/api/v1/admin/users/{admin.id}/status/",
        {"is_active": False},
        format="json",
    )
    assert self_toggle.status_code == 400
