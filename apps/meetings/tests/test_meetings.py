import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APIClient

from apps.meetings.models import Meeting

User = get_user_model()


@pytest.mark.django_db
def test_meeting_lifecycle_and_permissions():
    host = User.objects.create_user(username="host1", email="host1@example.com", password="Password123!")
    guest = User.objects.create_user(username="guest1", email="guest1@example.com", password="Password123!")

    host_client = APIClient()
    host_client.force_authenticate(user=host)

    guest_client = APIClient()
    guest_client.force_authenticate(user=guest)

    # Create meeting
    create_res = host_client.post("/api/v1/meetings/", {"title": "Architecture Sync"}, format="json")
    assert create_res.status_code == 201
    code = create_res.data["meeting_code"]
    assert code.startswith("avr-")
    assert create_res.data["is_host"] is True
    assert host_client.get(f"/api/v1/meetings/{code}/").status_code == 200
    assert guest_client.get(f"/api/v1/meetings/{code}/").status_code == 403

    # Guest cannot access ICE config before joining
    ice_denied = guest_client.get(f"/api/v1/meetings/{code}/ice-servers/")
    assert ice_denied.status_code == 403

    # Guest joins meeting -> placed in waiting room (202)
    waiting_res = guest_client.post(f"/api/v1/meetings/{code}/join/")
    assert waiting_res.status_code == 202
    assert waiting_res.data["admission_status"] == "waiting"

    # Guest still cannot access ICE config while waiting
    ice_waiting = guest_client.get(f"/api/v1/meetings/{code}/ice-servers/")
    assert ice_waiting.status_code == 403

    # Host admits guest
    admit_res = host_client.post(
        f"/api/v1/meetings/{code}/admissions/",
        {"user_id": guest.id, "decision": "admit"},
        format="json",
    )
    assert admit_res.status_code == 200
    assert admit_res.data["admission_status"] == "admitted"

    # Admitted guest enters meeting
    join_res = guest_client.post(f"/api/v1/meetings/{code}/join/")
    assert join_res.status_code == 200
    assert join_res.data["admission_status"] == "admitted"
    assert len(join_res.data["participants"]) == 2
    assert guest_client.get(f"/api/v1/meetings/{code}/").status_code == 200

    # Guest now authorized for ICE servers
    ice_ok = guest_client.get(f"/api/v1/meetings/{code}/ice-servers/")
    assert ice_ok.status_code == 200
    assert len(ice_ok.data["ice_servers"]) >= 1

    # Guest cannot end host's meeting
    end_denied = guest_client.post(f"/api/v1/meetings/{code}/end/")
    assert end_denied.status_code == 403

    # Host ends meeting
    end_ok = host_client.post(f"/api/v1/meetings/{code}/end/")
    assert end_ok.status_code == 200
    assert end_ok.data["status"] == "ended"

    # Joining ended meeting fails
    rejoin = guest_client.post(f"/api/v1/meetings/{code}/join/")
    assert rejoin.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_meeting_join_with_nullable_screen_sharer_on_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip(
            "This regression requires PostgreSQL row-lock behavior."
        )

    host = User.objects.create_user(
        username="lock_host", email="lock_host@example.com", password="Password123!"
    )
    guest = User.objects.create_user(
        username="lock_guest", email="lock_guest@example.com", password="Password123!"
    )
    meeting = Meeting.objects.create(
        title="Nullable screen sharer lock regression",
        host=host,
        active_screen_sharer=None,
    )

    client = APIClient()
    client.force_authenticate(user=guest)
    response = client.post(f"/api/v1/meetings/{meeting.meeting_code}/join/")

    assert response.status_code == 202
    assert response.data["admission_status"] == "waiting"
