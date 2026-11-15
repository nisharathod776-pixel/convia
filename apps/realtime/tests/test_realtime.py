import pytest
import socketio.exceptions
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

from apps.meetings.models import Meeting, MeetingParticipant
from apps.realtime.server import _authenticate_jwt, connect
from apps.realtime.validators import get_authorized_meeting_context

User = get_user_model()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_socketio_jwt_auth_and_room_validation():
    user = await User.objects.acreate(username="rtuser", email="rt@example.com", is_active=True)
    await sync_to_async(user.set_password)("Password123!")
    await sync_to_async(user.save)()

    outsider = await User.objects.acreate(username="rtout", email="rtout@example.com", is_active=True)
    meeting = await Meeting.objects.acreate(title="Realtime Room", host=user)
    await MeetingParticipant.objects.acreate(meeting=meeting, user=user, role="host", is_active=True)

    refresh = await sync_to_async(RefreshToken.for_user)(user)
    token = str(refresh.access_token)
    auth_user = await _authenticate_jwt(token)
    assert auth_user is not None
    assert auth_user["username"] == "rtuser"

    # Unauthenticated socket handshake refused
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await connect("sid-bad", {}, {"token": "invalid.jwt.token"})

    # Authorized member granted room context
    m_obj, is_host, err = await get_authorized_meeting_context(user.id, meeting.meeting_code)
    assert err is None
    assert m_obj.id == meeting.id
    assert is_host is True

    # Non-member rejected from joining unauthorized room
    m_obj2, _, err2 = await get_authorized_meeting_context(outsider.id, meeting.meeting_code)
    assert m_obj2 is None
    assert err2 is not None
