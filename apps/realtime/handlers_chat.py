from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.meetings.models import ActivityLog, Meeting, MeetingChatMessage, MeetingParticipant
from apps.meetings.permissions import can_delete_chat_message, is_meeting_teacher
from .server import sio
from .validators import (
    check_chat_permission,
    get_peer_breakout_id,
    room_subchannel,
    verify_sid_in_room,
)

User = get_user_model()


@sync_to_async
def _persist_chat_message(meeting_code: str, user_id: int, text: str, breakout_room_id=None):
    user = User.objects.filter(id=user_id, is_active=True).first()
    meeting = Meeting.objects.select_related("host").filter(meeting_code=meeting_code.lower()).first()
    if not user or not meeting or meeting.status == Meeting.STATUS_ENDED:
        return None
    sender_role = (
        MeetingParticipant.ROLE_TEACHER
        if is_meeting_teacher(user, meeting)
        else MeetingParticipant.ROLE_STUDENT
    )
    msg = MeetingChatMessage.objects.create(
        meeting=meeting,
        sender=user,
        sender_role=sender_role,
        breakout_room_id=breakout_room_id or None,
        text=text[:2000],
    )
    return {
        "id": str(msg.id),
        "sender_id": user.id,
        "sender_username": user.username,
        "sender_name": user.full_name or user.username,
        "sender_role": msg.sender_role,
        "breakout_room_id": str(msg.breakout_room_id) if msg.breakout_room_id else None,
        "text": msg.text,
        "timestamp": msg.created_at.isoformat(),
    }


@sync_to_async
def _delete_chat_message(meeting_code: str, user_id: int, message_id: str):
    if not message_id:
        return False, "Message ID is required.", None
    try:
        with transaction.atomic():
            user = User.objects.filter(id=user_id, is_active=True).first()
            meeting = Meeting.objects.select_related("host").filter(meeting_code=meeting_code.lower()).first()
            if not user or not meeting:
                return False, "Meeting not found.", None
            msg = (
                MeetingChatMessage.objects.select_for_update()
                .select_related("sender")
                .filter(id=message_id, meeting=meeting)
                .first()
            )
            if not msg or msg.is_deleted:
                return False, "Message not found or already deleted.", None
            if not can_delete_chat_message(user, meeting, msg):
                ActivityLog.objects.create(
                    user=user,
                    action="unauthorized_chat_delete",
                    detail=f"{meeting.meeting_code}: msg={msg.id}",
                )
                return False, "You do not have permission to delete this message.", None
            msg.is_deleted = True
            msg.deleted_by = user
            msg.deleted_at = timezone.now()
            msg.save(update_fields=["is_deleted", "deleted_by", "deleted_at"])
            ActivityLog.objects.create(
                user=user,
                action="chat_message_deleted",
                detail=f"{meeting.meeting_code}: msg={msg.id} sender=@{msg.sender.username}",
            )
            return True, str(msg.id), str(msg.breakout_room_id) if msg.breakout_room_id else None
    except Exception:
        return False, "Invalid message ID.", None


@sio.event
async def chat_message(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return

    allowed, err = await check_chat_permission(user_data["user_id"], code)
    if not allowed:
        await sio.emit("error_event", {"message": err}, to=sid)
        return

    text = str(data.get("text", "")).strip()
    if not text:
        return

    breakout_room_id = get_peer_breakout_id(code, sid)
    payload = await _persist_chat_message(code, user_data["user_id"], text, breakout_room_id)
    if not payload:
        return

    await sio.emit("chat_message", payload, room=room_subchannel(code, breakout_room_id))


@sio.event
async def delete_chat_message(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return

    message_id = str(data.get("message_id", "")).strip()
    ok, result, breakout_id = await _delete_chat_message(code, user_data["user_id"], message_id)
    if not ok:
        await sio.emit("error_event", {"message": result}, to=sid)
        return

    await sio.emit(
        "chat_message_deleted",
        {
            "meeting_code": code,
            "message_id": result,
            "breakout_room_id": breakout_id,
            "deleted_by_id": user_data["user_id"],
        },
        room=room_subchannel(code, breakout_id),
    )
