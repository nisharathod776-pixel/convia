import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.fileshare.models import SharedFile
from apps.meetings.models import Meeting

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


@pytest.mark.django_db
def test_admin_overview_statistics_use_database_records(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    admin = User.objects.create_user(
        username="statsadmin",
        email="statsadmin@example.com",
        password="Password123!",
        is_staff=True,
    )
    member = User.objects.create_user(username="statsmember", email="statsmember@example.com", password="Password123!")
    deactivated = User.objects.create_user(
        username="statsinactive",
        email="statsinactive@example.com",
        password="Password123!",
        is_active=False,
    )
    Meeting.objects.create(title="Active room", host=member)
    ended_meeting = Meeting.objects.create(title="Completed room", host=member, status=Meeting.STATUS_ENDED)
    SharedFile.objects.create(
        meeting=ended_meeting,
        uploaded_by=member,
        file=SimpleUploadedFile("stats.txt", b"four", content_type="text/plain"),
        original_name="stats.txt",
        file_size=4,
        mime_type="text/plain",
    )

    client = APIClient()
    client.force_authenticate(user=admin)
    response = client.get("/api/v1/admin/overview/")

    assert response.status_code == 200
    assert response.data["stats"] == {
        "total_users": 3,
        "active_users": 2,
        "deactivated_users": 1,
        "total_meetings": 2,
        "active_meetings": 1,
        "completed_meetings": 1,
        "total_files": 1,
        "storage_bytes": 4,
    }


@pytest.mark.django_db
def test_admin_overview_rejects_unauthenticated_requests():
    response = APIClient().get("/api/v1/admin/overview/")
    assert response.status_code == 401
