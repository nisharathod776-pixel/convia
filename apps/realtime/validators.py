from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.meetings.models import Meeting
from apps.meetings.permissions import is_meeting_member
from .server import sio

User = get_user_model()

# Active connected peers per meeting_code: { meeting_code: { sid: peer_dict } }
ROOM_PEERS: dict[str, dict[str, dict]] = {}


def room_channel(meeting_code: str) -> str:
    return f"meeting:{meeting_code.lower()}"


@sync_to_async
def get_authorized_meeting_context(user_id: int, meeting_code: str):
    code = (meeting_code or "").strip().lower()
    if not code:
        return None, False, "Meeting code is required."
    try:
        user = User.objects.get(id=user_id, is_active=True)
    except User.DoesNotExist:
        return None, False, "User account is inactive."

    try:
        meeting = Meeting.objects.select_related("host").get(meeting_code=code)
    except Meeting.DoesNotExist:
        return None, False, "Meeting not found."

    if meeting.status == Meeting.STATUS_ENDED:
        return None, False, "Meeting has ended."

    if not is_meeting_member(user, meeting):
        return None, False, "Not authorized for this meeting room."

    return meeting, (meeting.host_id == user.id), None


async def verify_sid_in_room(sid: str, meeting_code: str):
    code = (meeting_code or "").strip().lower()
    if not code:
        return None, None
    session = await sio.get_session(sid)
    if not session:
        return None, None
    if code not in session.get("rooms", set()):
        return None, None
    if code not in ROOM_PEERS or sid not in ROOM_PEERS[code]:
        return None, None
    return code, session["user"]
