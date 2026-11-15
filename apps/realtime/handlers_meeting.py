import logging
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.fileshare.models import SharedFile
from apps.meetings.models import Meeting, MeetingParticipant
from apps.whiteboard.models import WhiteboardOperation
from .server import sio
from .validators import ROOM_PEERS, get_authorized_meeting_context, room_channel, verify_sid_in_room

logger = logging.getLogger("apps.realtime")


@sync_to_async
def _load_whiteboard_ops(meeting_id):
    return list(WhiteboardOperation.objects.filter(meeting_id=meeting_id).values_list("payload", flat=True))


@sync_to_async
def _mark_meeting_ended(meeting_code: str, user_id: int, is_admin: bool):
    try:
        meeting = Meeting.objects.get(meeting_code=meeting_code)
    except Meeting.DoesNotExist:
        return False
    if meeting.host_id != user_id and not is_admin:
        return False
    now = timezone.now()
    if meeting.status != Meeting.STATUS_ENDED:
        meeting.status = Meeting.STATUS_ENDED
        meeting.ended_at = now
        meeting.save(update_fields=["status", "ended_at"])
        MeetingParticipant.objects.filter(meeting=meeting, is_active=True).update(is_active=False, left_at=now)
    return True


@sync_to_async
def _verify_file_in_meeting(file_id: str, meeting_code: str) -> bool:
    if not file_id:
        return False
    return SharedFile.objects.filter(id=file_id, meeting__meeting_code=meeting_code).exists()


async def _leave_all_rooms(sid: str):
    session = await sio.get_session(sid)
    if not session:
        return
    rooms = list(session.get("rooms", set()))
    for code in rooms:
        await _remove_sid_from_room(sid, code)
    session["rooms"] = set()
    await sio.save_session(sid, session)


async def _remove_sid_from_room(sid: str, code: str):
    channel = room_channel(code)
    await sio.leave_room(sid, channel)
    peers = ROOM_PEERS.get(code)
    if peers and sid in peers:
        departed = peers.pop(sid)
        if not peers:
            ROOM_PEERS.pop(code, None)
        await sio.emit(
            "participant_left",
            {"sid": sid, "username": departed.get("username")},
            room=channel,
            skip_sid=sid,
        )


@sio.event
async def join_meeting(sid, data):
    session = await sio.get_session(sid)
    if not session:
        return
    user_data = session["user"]
    code = (data or {}).get("meeting_code", "").strip().lower()

    meeting, is_host, err = await get_authorized_meeting_context(user_data["user_id"], code)
    if err or not meeting:
        await sio.emit("error_event", {"message": err or "Unable to join room."}, to=sid)
        return

    channel = room_channel(code)
    await sio.enter_room(sid, channel)
    session["rooms"].add(code)
    await sio.save_session(sid, session)

    peer_info = {
        "sid": sid,
        "user_id": user_data["user_id"],
        "username": user_data["username"],
        "full_name": user_data["full_name"],
        "is_host": is_host,
        "mic_enabled": True,
        "cam_enabled": True,
        "screen_sharing": False,
    }
    ROOM_PEERS.setdefault(code, {})[sid] = peer_info

    wb_ops = await _load_whiteboard_ops(meeting.id)
    existing_peers = [p for peer_sid, p in ROOM_PEERS[code].items() if peer_sid != sid]

    await sio.emit(
        "room_joined",
        {
            "self_sid": sid,
            "meeting_code": code,
            "peers": existing_peers,
            "whiteboard_ops": wb_ops,
        },
        to=sid,
    )
    await sio.emit("participant_joined", {"participant": peer_info}, room=channel, skip_sid=sid)
    logger.info("User %s joined room %s (sid=%s)", user_data["username"], code, sid)


@sio.event
async def leave_meeting(sid, data):
    code = ((data or {}).get("meeting_code") or "").strip().lower()
    session = await sio.get_session(sid)
    if not session:
        return
    if code and code in session.get("rooms", set()):
        session["rooms"].discard(code)
        await sio.save_session(sid, session)
        await _remove_sid_from_room(sid, code)


@sio.event
async def end_meeting(sid, data):
    code, user_data = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        return
    is_admin = bool(user_data.get("is_staff") or user_data.get("role") == "admin")
    ok = await _mark_meeting_ended(code, user_data["user_id"], is_admin)
    if not ok:
        await sio.emit("error_event", {"message": "Only the host can end this meeting."}, to=sid)
        return

    channel = room_channel(code)
    await sio.emit("meeting_ended", {"meeting_code": code}, room=channel)
    ROOM_PEERS.pop(code, None)


@sio.event
async def file_shared(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    file_obj = (data or {}).get("file") or {}
    if await _verify_file_in_meeting(str(file_obj.get("id", "")), code):
        await sio.emit("file_shared", {"file": file_obj}, room=room_channel(code), skip_sid=sid)


@sio.event
async def disconnect(sid):
    await _leave_all_rooms(sid)
    logger.info("Socket disconnected sid=%s", sid)
