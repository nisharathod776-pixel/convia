import asyncio
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.meetings.models import ActivityLog, BreakoutRoom, Meeting, MeetingParticipant
from apps.meetings.permissions import (
    can_clear_whiteboard,
    can_edit_whiteboard,
    can_send_chat,
    is_meeting_teacher,
)
from apps.meetings.services import admit_meeting_participant
from .server import sio

User = get_user_model()

# Active connected peers per meeting_code: { meeting_code: { sid: peer_dict } }
ROOM_PEERS: dict[str, dict[str, dict]] = {}
# Per-meeting async locks to serialize screen-share, moderation, breakout, and admission mutations
ROOM_LOCKS: dict[str, asyncio.Lock] = {}


def get_room_lock(meeting_code: str) -> asyncio.Lock:
    code = (meeting_code or "").strip().lower()
    lock = ROOM_LOCKS.get(code)
    if lock is None:
        lock = asyncio.Lock()
        ROOM_LOCKS[code] = lock
    return lock


def room_channel(meeting_code: str) -> str:
    return f"meeting:{meeting_code.lower()}"


def control_channel(meeting_code: str) -> str:
    return f"control:{meeting_code.lower()}"


def breakout_channel(meeting_code: str, breakout_id) -> str:
    return f"breakout:{meeting_code.lower()}:{str(breakout_id)}"


def room_subchannel(meeting_code: str, breakout_id=None) -> str:
    if breakout_id:
        return breakout_channel(meeting_code, breakout_id)
    return room_channel(meeting_code)


def get_peer_breakout_id(meeting_code: str, sid: str):
    code = (meeting_code or "").strip().lower()
    peer = ROOM_PEERS.get(code, {}).get(sid)
    if not peer:
        return None
    return peer.get("breakout_room_id") or None


def get_peer_subchannel(meeting_code: str, sid: str) -> str:
    return room_subchannel(meeting_code, get_peer_breakout_id(meeting_code, sid))


def same_subchannel(meeting_code: str, sid1: str, sid2: str) -> bool:
    code = (meeting_code or "").strip().lower()
    peers = ROOM_PEERS.get(code, {})
    if sid1 not in peers or sid2 not in peers:
        return False
    return (peers[sid1].get("breakout_room_id") or None) == (peers[sid2].get("breakout_room_id") or None)


@sync_to_async
def get_authorized_meeting_context(user_id: int, meeting_code: str):
    code = (meeting_code or "").strip().lower()
    if not code:
        return None, False, "Meeting code is required."
    try:
        user = User.objects.get(id=user_id, is_active=True)
    except User.DoesNotExist:
        return None, False, "User account is inactive."

    with transaction.atomic():
        try:
            meeting = Meeting.objects.select_for_update(of=("self",)).select_related(
                "host", "active_screen_sharer"
            ).get(meeting_code=code)
        except Meeting.DoesNotExist:
            return None, False, "Meeting not found."

        _, error = admit_meeting_participant(user, meeting)
        if error:
            return None, False, error["detail"]

        return meeting, is_meeting_teacher(user, meeting), None


@sync_to_async
def check_whiteboard_permission(
    user_id: int, meeting_code: str, require_clear: bool = False, breakout_room_id=None
):
    try:
        user = User.objects.get(id=user_id, is_active=True)
        meeting = Meeting.objects.select_related("host").get(meeting_code=meeting_code.lower())
    except (User.DoesNotExist, Meeting.DoesNotExist):
        return False
    if meeting.status == Meeting.STATUS_ENDED:
        return False
    if require_clear:
        return can_clear_whiteboard(user, meeting)
    breakout_room = None
    if breakout_room_id:
        breakout_room = BreakoutRoom.objects.filter(id=breakout_room_id, meeting=meeting).first()
    return can_edit_whiteboard(user, meeting, breakout_room=breakout_room)


@sync_to_async
def check_chat_permission(user_id: int, meeting_code: str):
    try:
        user = User.objects.get(id=user_id, is_active=True)
        meeting = Meeting.objects.select_related("host").get(meeting_code=meeting_code.lower())
    except (User.DoesNotExist, Meeting.DoesNotExist):
        return False, "Meeting not found."
    if meeting.status == Meeting.STATUS_ENDED:
        return False, "Meeting has ended."
    participant = MeetingParticipant.objects.filter(meeting=meeting, user=user).first()
    if can_send_chat(user, meeting, participant):
        return True, None
    if meeting.chat_mode == Meeting.CHAT_DISABLED:
        reason = "Chat has been disabled by the teacher."
    elif meeting.chat_mode == Meeting.CHAT_TEACHER_ONLY:
        reason = "Chat is currently restricted to the teacher only."
    elif participant and participant.chat_muted:
        reason = "Your chat permissions have been muted by the teacher."
    else:
        reason = "You do not have permission to send messages in chat."
    ActivityLog.objects.create(
        user=user,
        action="unauthorized_chat_message",
        detail=f"{meeting.meeting_code}: {reason}",
    )
    return False, reason


@sync_to_async
def check_is_teacher(user_id: int, meeting_code: str) -> bool:
    try:
        user = User.objects.get(id=user_id, is_active=True)
        meeting = Meeting.objects.select_related("host").get(meeting_code=meeting_code.lower())
    except (User.DoesNotExist, Meeting.DoesNotExist):
        return False
    return is_meeting_teacher(user, meeting)


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


async def verify_teacher_in_room(sid: str, meeting_code: str):
    code, user_data = await verify_sid_in_room(sid, meeting_code)
    if not code or not user_data:
        return None, None
    if not await check_is_teacher(user_data["user_id"], code):
        return None, None
    return code, user_data
