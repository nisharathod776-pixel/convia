import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.meetings.models import Meeting, MeetingParticipant
from apps.whiteboard.models import WhiteboardOperation

User = get_user_model()


@pytest.mark.django_db
def test_whiteboard_state_recovery_and_clear():
    host = User.objects.create_user(username="wbhost", email="wb@example.com", password="Password123!")
    outsider = User.objects.create_user(username="wbout", email="wbout@example.com", password="Password123!")

    meeting = Meeting.objects.create(title="Design Review", host=host)
    MeetingParticipant.objects.create(meeting=meeting, user=host, role="host", is_active=True)

    WhiteboardOperation.objects.create(
        meeting=meeting,
        user=host,
        op_id="op-1",
        op_type="draw",
        payload={"id": "op-1", "type": "draw", "x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5, "color": "#0F172A", "size": 4},
    )

    host_client = APIClient()
    host_client.force_authenticate(user=host)
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)

    assert outsider_client.get(f"/api/v1/meetings/{meeting.meeting_code}/whiteboard/").status_code == 403

    state_res = host_client.get(f"/api/v1/meetings/{meeting.meeting_code}/whiteboard/")
    assert state_res.status_code == 200
    assert len(state_res.data["operations"]) == 1

    clear_res = host_client.delete(f"/api/v1/meetings/{meeting.meeting_code}/whiteboard/")
    assert clear_res.status_code == 200
    assert WhiteboardOperation.objects.filter(meeting=meeting).count() == 0
