import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

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

    # Guest cannot access ICE config before joining
    ice_denied = guest_client.get(f"/api/v1/meetings/{code}/ice-servers/")
    assert ice_denied.status_code == 403

    # Guest joins meeting
    join_res = guest_client.post(f"/api/v1/meetings/{code}/join/")
    assert join_res.status_code == 200
    assert len(join_res.data["participants"]) == 2

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
