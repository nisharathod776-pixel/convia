import asyncio
import logging
from datetime import timedelta
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.fileshare.models import SharedFile
from apps.meetings.models import (
    ActivityLog,
    BreakoutRoom,
    Meeting,
    MeetingChatMessage,
    MeetingParticipant,
)
from apps.meetings.permissions import (
    can_edit_whiteboard,
    is_meeting_teacher,
)
from apps.meetings.services import (
    close_all_attendance_sessions,
    close_user_attendance_sessions,
    expire_breakout_rooms,
    open_attendance_session,
)
from apps.whiteboard.models import WhiteboardOperation
from .server import sio
from .validators import (
    ROOM_PEERS,
    WAITING_PEERS,
    control_channel,
    get_authorized_meeting_context,
    get_peer_subchannel,
    get_room_lock,
    get_waiting_meeting_context,
    room_channel,
    room_subchannel,
    verify_sid_in_room,
    verify_teacher_in_room,
    waiting_channel,
)

User = get_user_model()
logger = logging.getLogger("apps.realtime")
BREAKOUT_EXPIRY_POLL_SECONDS = 1
_breakout_expiry_task = None


def _serialize_breakout_rooms_sync(meeting: Meeting) -> list[dict]:
    rooms = meeting.breakout_rooms.prefetch_related("assigned_participants__user").order_by("created_at")
    result = []
    for r in rooms:
        assigned = [
            {
                "user_id": p.user_id,
                "username": p.user.username,
                "full_name": p.user.full_name or p.user.username,
                "is_active": p.is_active,
            }
            for p in r.assigned_participants.all()
            if p.admission_status == MeetingParticipant.ADMISSION_ADMITTED
        ]
        result.append(
            {
                "id": str(r.id),
                "name": r.name,
                "status": r.status,
                "duration_minutes": r.duration_minutes,
                "allow_return_to_main": r.allow_return_to_main,
                "opened_at": r.opened_at.isoformat() if r.opened_at else None,
                "ends_at": r.ends_at.isoformat() if r.ends_at else None,
                "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                "assigned_users": assigned,
            }
        )
    return result


@sync_to_async
def _expire_due_breakouts_db():
    now = timezone.now()
    due_meetings = Meeting.objects.filter(
        breakout_rooms__status=BreakoutRoom.STATUS_OPEN,
        breakout_rooms__ends_at__lte=now,
    ).distinct()
    updates = []
    for meeting in due_meetings:
        expired_ids = expire_breakout_rooms(meeting, now)
        if expired_ids:
            updates.append(
                {
                    "meeting_code": meeting.meeting_code,
                    "expired_ids": expired_ids,
                    "breakout_rooms": _serialize_breakout_rooms_sync(meeting),
                }
            )
    return updates


@sync_to_async
def _closed_breakout_ids_db(room_ids):
    if not room_ids:
        return set()
    return {
        str(room_id)
        for room_id in BreakoutRoom.objects.filter(
            id__in=room_ids,
            status=BreakoutRoom.STATUS_CLOSED,
        ).values_list("id", flat=True)
    }


@sync_to_async
def _breakout_rooms_for_meeting_db(meeting_code):
    meeting = Meeting.objects.filter(meeting_code=meeting_code).first()
    return _serialize_breakout_rooms_sync(meeting) if meeting else []


async def _expire_breakouts_once():
    updates = {}
    for update in await _expire_due_breakouts_db():
        updates[update["meeting_code"]] = {
            "expired_ids": set(update["expired_ids"]),
            "breakout_rooms": update["breakout_rooms"],
        }

    for meeting_code, peers in list(ROOM_PEERS.items()):
        local_room_ids = {
            str(peer["breakout_room_id"])
            for peer in peers.values()
            if peer.get("breakout_room_id")
        }
        closed_ids = await _closed_breakout_ids_db(local_room_ids)
        if not closed_ids:
            continue
        update = updates.setdefault(
            meeting_code,
            {"expired_ids": set(), "breakout_rooms": None},
        )
        update["expired_ids"].update(closed_ids)
        if update["breakout_rooms"] is None:
            update["breakout_rooms"] = await _breakout_rooms_for_meeting_db(meeting_code)

    for meeting_code, update in updates.items():
        async with get_room_lock(meeting_code):
            for sid, peer in list(ROOM_PEERS.get(meeting_code, {}).items()):
                breakout_id = str(peer.get("breakout_room_id") or "")
                if breakout_id in update["expired_ids"]:
                    await _move_peer_subchannel(meeting_code, sid, None)
            await sio.emit(
                "breakout_state_updated",
                {"meeting_code": meeting_code, "breakout_rooms": update["breakout_rooms"]},
                room=control_channel(meeting_code),
            )


async def _breakout_expiry_loop():
    while True:
        try:
            await _expire_breakouts_once()
        except Exception:
            logger.exception("Breakout expiry scheduler cycle failed.")
        await asyncio.sleep(BREAKOUT_EXPIRY_POLL_SECONDS)


async def start_breakout_expiry_scheduler():
    global _breakout_expiry_task
    if _breakout_expiry_task and not _breakout_expiry_task.done():
        return
    _breakout_expiry_task = asyncio.create_task(
        _breakout_expiry_loop(),
        name="convia-breakout-expiry",
    )
    logger.info("Breakout expiry scheduler started.")


async def stop_breakout_expiry_scheduler():
    global _breakout_expiry_task
    task = _breakout_expiry_task
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _breakout_expiry_task = None
    logger.info("Breakout expiry scheduler stopped.")


def _permissions_dict_sync(meeting: Meeting, user=None, is_teacher: bool = False, breakout_room=None) -> dict:
    if user is not None:
        can_draw = can_edit_whiteboard(user, meeting, breakout_room=breakout_room)
    else:
        can_draw = is_teacher or bool(meeting.whiteboard_student_write_enabled)
    return {
        "whiteboard_student_write_enabled": meeting.whiteboard_student_write_enabled,
        "allow_student_screenshare": meeting.allow_student_screenshare,
        "waiting_room_enabled": meeting.waiting_room_enabled,
        "allow_student_mic": meeting.allow_student_mic,
        "allow_student_camera": meeting.allow_student_camera,
        "allow_student_file_share": meeting.allow_student_file_share,
        "chat_mode": meeting.chat_mode,
        "can_draw_whiteboard": can_draw,
        "can_clear_whiteboard": is_teacher,
        "active_screen_sharer_id": meeting.active_screen_sharer_id,
        "active_screen_sharer_username": (
            meeting.active_screen_sharer.username if meeting.active_screen_sharer else None
        ),
        "active_screen_sharer_sid": meeting.active_screen_sharer_sid,
        "admission_deadline": meeting.admission_deadline.isoformat() if meeting.admission_deadline else None,
        "is_recording": meeting.is_recording,
        "recording_started_at": meeting.recording_started_at.isoformat() if meeting.recording_started_at else None,
    }


@sync_to_async
def _enter_meeting_and_load_state(meeting_id, user_id: int, is_teacher: bool):
    now = timezone.now()
    with transaction.atomic():
        user = User.objects.get(id=user_id)
        meeting = Meeting.objects.select_related("host", "active_screen_sharer").get(id=meeting_id)
        expire_breakout_rooms(meeting, now)
        if meeting.status == Meeting.STATUS_SCHEDULED:
            meeting.status = Meeting.STATUS_ACTIVE
            meeting.save(update_fields=["status"])

        role = MeetingParticipant.ROLE_TEACHER if is_teacher else MeetingParticipant.ROLE_STUDENT
        participant, created = MeetingParticipant.objects.select_for_update().select_related("current_breakout").get_or_create(
            meeting=meeting,
            user=user,
            defaults={
                "role": role,
                "admission_status": MeetingParticipant.ADMISSION_ADMITTED,
                "is_active": True,
                "admitted_at": now,
            },
        )
        if not created:
            participant.is_active = True
            participant.left_at = None
            if not participant.admitted_at:
                participant.admitted_at = now
            participant.save(update_fields=["is_active", "left_at", "admitted_at"])

        open_attendance_session(meeting, user, role, at_time=now)

        breakout_room = None
        if participant.current_breakout and participant.current_breakout.status == BreakoutRoom.STATUS_OPEN:
            breakout_room = participant.current_breakout
        breakout_room_id = str(breakout_room.id) if breakout_room else None

        wb_qs = WhiteboardOperation.objects.filter(meeting_id=meeting_id)
        if breakout_room_id:
            wb_qs = wb_qs.filter(breakout_room_id=breakout_room_id)
        else:
            wb_qs = wb_qs.filter(breakout_room__isnull=True)
        wb_ops = list(wb_qs.values_list("payload", flat=True))

        chat_qs = MeetingChatMessage.objects.filter(meeting_id=meeting_id, is_deleted=False).select_related("sender")
        if breakout_room_id:
            chat_qs = chat_qs.filter(breakout_room_id=breakout_room_id)
        else:
            chat_qs = chat_qs.filter(breakout_room__isnull=True)
        chat_qs = chat_qs.order_by("created_at")[:200]
        chat_messages = [
            {
                "id": str(m.id),
                "sender_id": m.sender_id,
                "sender_username": m.sender.username,
                "sender_name": m.sender.full_name or m.sender.username,
                "sender_role": m.sender_role,
                "breakout_room_id": str(m.breakout_room_id) if m.breakout_room_id else None,
                "text": m.text,
                "timestamp": m.created_at.isoformat(),
            }
            for m in chat_qs
        ]

        waiting_room = []
        if meeting.host_id == user.id:
            waiting_qs = (
                MeetingParticipant.objects.filter(
                    meeting_id=meeting_id,
                    admission_status=MeetingParticipant.ADMISSION_WAITING,
                )
                .select_related("user")
                .order_by("requested_at", "joined_at")
            )
            waiting_room = [
                {
                    "id": p.id,
                    "user_id": p.user_id,
                    "username": p.user.username,
                    "full_name": p.user.full_name or p.user.username,
                    "admission_status": p.admission_status,
                    "requested_at": p.requested_at.isoformat() if p.requested_at else None,
                }
                for p in waiting_qs
            ]

        permissions = _permissions_dict_sync(meeting, user=user, is_teacher=is_teacher, breakout_room=breakout_room)
        breakout_rooms = _serialize_breakout_rooms_sync(meeting)
        my_moderation = {
            "mic_locked": (not is_teacher) and (not meeting.allow_student_mic or participant.mic_locked),
            "camera_locked": (not is_teacher) and (not meeting.allow_student_camera or participant.camera_locked),
            "chat_muted": (not is_teacher) and participant.chat_muted,
            "hand_raised": participant.hand_raised,
            "current_breakout_id": breakout_room_id,
        }
        return wb_ops, chat_messages, waiting_room, permissions, my_moderation, breakout_rooms, breakout_room_id


@sync_to_async
def _load_subchannel_state(meeting_code: str, user_id: int, breakout_room_id=None):
    user = User.objects.filter(id=user_id, is_active=True).first()
    meeting = Meeting.objects.select_related("host", "active_screen_sharer").filter(meeting_code=meeting_code.lower()).first()
    if not user or not meeting:
        return [], [], {}, [], None
    is_teacher = is_meeting_teacher(user, meeting)
    breakout_room = None
    if breakout_room_id:
        breakout_room = BreakoutRoom.objects.filter(id=breakout_room_id, meeting=meeting).first()

    wb_qs = WhiteboardOperation.objects.filter(meeting=meeting)
    if breakout_room_id:
        wb_qs = wb_qs.filter(breakout_room_id=breakout_room_id)
    else:
        wb_qs = wb_qs.filter(breakout_room__isnull=True)
    wb_ops = list(wb_qs.values_list("payload", flat=True))

    chat_qs = MeetingChatMessage.objects.filter(meeting=meeting, is_deleted=False).select_related("sender")
    if breakout_room_id:
        chat_qs = chat_qs.filter(breakout_room_id=breakout_room_id)
    else:
        chat_qs = chat_qs.filter(breakout_room__isnull=True)
    chat_messages = [
        {
            "id": str(m.id),
            "sender_id": m.sender_id,
            "sender_username": m.sender.username,
            "sender_name": m.sender.full_name or m.sender.username,
            "sender_role": m.sender_role,
            "breakout_room_id": str(m.breakout_room_id) if m.breakout_room_id else None,
            "text": m.text,
            "timestamp": m.created_at.isoformat(),
        }
        for m in chat_qs.order_by("created_at")[:200]
    ]
    permissions = _permissions_dict_sync(meeting, user=user, is_teacher=is_teacher, breakout_room=breakout_room)
    breakout_rooms = _serialize_breakout_rooms_sync(meeting)
    room_dict = None
    if breakout_room:
        room_dict = {
            "id": str(breakout_room.id),
            "name": breakout_room.name,
            "status": breakout_room.status,
            "duration_minutes": breakout_room.duration_minutes,
            "allow_return_to_main": breakout_room.allow_return_to_main,
            "ends_at": breakout_room.ends_at.isoformat() if breakout_room.ends_at else None,
        }
    return wb_ops, chat_messages, permissions, breakout_rooms, room_dict


@sync_to_async
def _close_disconnected_attendance(meeting_code: str, user_id: int):
    now = timezone.now()
    with transaction.atomic():
        meeting = Meeting.objects.filter(meeting_code=meeting_code.lower()).first()
        if not meeting:
            return
        MeetingParticipant.objects.filter(meeting=meeting, user_id=user_id, is_active=True).update(
            is_active=False,
            left_at=now,
            hand_raised=False,
        )
        close_user_attendance_sessions(meeting, user_id, at_time=now)


@sync_to_async
def _get_waiting_room_list(meeting_code: str):
    waiting_qs = (
        MeetingParticipant.objects.filter(
            meeting__meeting_code=meeting_code.lower(),
            admission_status=MeetingParticipant.ADMISSION_WAITING,
        )
        .select_related("user")
        .order_by("requested_at", "joined_at")
    )
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "username": p.user.username,
            "full_name": p.user.full_name or p.user.username,
            "admission_status": p.admission_status,
            "requested_at": p.requested_at.isoformat() if p.requested_at else None,
        }
        for p in waiting_qs
    ]


@sync_to_async
def _process_admission_decision(meeting_code: str, teacher_id: int, target_user_id: int, decision: str):
    with transaction.atomic():
        teacher = User.objects.filter(id=teacher_id, is_active=True).first()
        meeting = Meeting.objects.select_for_update().filter(meeting_code=meeting_code.lower()).first()
        if not teacher or not meeting:
            return False, "Meeting not found.", []
        if meeting.host_id != teacher.id:
            ActivityLog.objects.create(
                user=teacher,
                action="unauthorized_admission_decision",
                detail=f"{meeting.meeting_code}: target={target_user_id}",
            )
            return False, "Only the Host can manage admission requests.", []
        if meeting.status == Meeting.STATUS_ENDED:
            return False, "Meeting has already ended.", []

        participant = (
            MeetingParticipant.objects.select_for_update()
            .select_related("user")
            .filter(meeting=meeting, user_id=target_user_id)
            .first()
        )
        if not participant:
            return False, "Admission request not found.", []
        if participant.is_teacher:
            return False, "Cannot modify admission status for the meeting teacher.", []
        if participant.admission_status != MeetingParticipant.ADMISSION_WAITING:
            return False, f"Request is already {participant.admission_status}.", []

        now = timezone.now()
        if decision == "admit":
            if meeting.status == Meeting.STATUS_SCHEDULED:
                meeting.status = Meeting.STATUS_ACTIVE
                meeting.save(update_fields=["status"])
            participant.admission_status = MeetingParticipant.ADMISSION_ADMITTED
            participant.is_active = True
            participant.admitted_at = now
            participant.left_at = None
            participant.decision_by = teacher
            participant.save(
                update_fields=["admission_status", "is_active", "admitted_at", "left_at", "decision_by"]
            )
            ActivityLog.objects.create(
                user=teacher,
                action="admission_admitted",
                detail=f"{meeting.meeting_code}: @{participant.user.username}",
            )
        else:
            participant.admission_status = MeetingParticipant.ADMISSION_REJECTED
            participant.is_active = False
            participant.rejected_at = now
            participant.decision_by = teacher
            participant.save(
                update_fields=["admission_status", "is_active", "rejected_at", "decision_by"]
            )
            ActivityLog.objects.create(
                user=teacher,
                action="admission_rejected",
                detail=f"{meeting.meeting_code}: @{participant.user.username}",
            )

        waiting_qs = (
            MeetingParticipant.objects.filter(
                meeting=meeting,
                admission_status=MeetingParticipant.ADMISSION_WAITING,
            )
            .select_related("user")
            .order_by("requested_at", "joined_at")
        )
        waiting_list = [
            {
                "id": p.id,
                "user_id": p.user_id,
                "username": p.user.username,
                "full_name": p.user.full_name or p.user.username,
                "admission_status": p.admission_status,
                "requested_at": p.requested_at.isoformat() if p.requested_at else None,
            }
            for p in waiting_qs
        ]
        return True, participant.admission_status, waiting_list


@sync_to_async
def _update_permissions_db(meeting_code: str, teacher_id: int, data: dict):
    with transaction.atomic():
        teacher = User.objects.filter(id=teacher_id, is_active=True).first()
        meeting = (
            Meeting.objects.select_for_update()
            .select_related("host", "active_screen_sharer")
            .filter(meeting_code=meeting_code.lower())
            .first()
        )
        if not teacher or not meeting:
            return False, "Meeting not found.", None, None
        if not is_meeting_teacher(teacher, meeting):
            ActivityLog.objects.create(
                user=teacher,
                action="unauthorized_permission_change",
                detail=meeting.meeting_code,
            )
            return False, "Only the meeting host can update permissions.", None, None

        updated_fields = []
        changes = []
        revoked_sid = None

        for bool_field, log_name in (
            ("whiteboard_student_write_enabled", "wb_student"),
            ("allow_student_screenshare", "student_screenshare"),
            ("waiting_room_enabled", "waiting_room"),
            ("allow_student_mic", "student_mic"),
            ("allow_student_camera", "student_camera"),
            ("allow_student_file_share", "student_file_share"),
        ):
            if bool_field in data and isinstance(data[bool_field], bool):
                val = data[bool_field]
                setattr(meeting, bool_field, val)
                updated_fields.append(bool_field)
                changes.append(f"{log_name}={val}")

                if (
                    bool_field == "allow_student_screenshare"
                    and not val
                    and meeting.active_screen_sharer_id
                    and meeting.active_screen_sharer_id != meeting.host_id
                ):
                    revoked_sid = meeting.active_screen_sharer_sid
                    meeting.active_screen_sharer = None
                    meeting.active_screen_sharer_sid = ""
                    updated_fields.extend(["active_screen_sharer", "active_screen_sharer_sid"])

        if "chat_mode" in data:
            mode = str(data.get("chat_mode", "")).strip()
            valid_modes = {choice[0] for choice in Meeting.CHAT_MODE_CHOICES}
            if mode not in valid_modes:
                return False, "Invalid chat_mode value.", None, None
            meeting.chat_mode = mode
            updated_fields.append("chat_mode")
            changes.append(f"chat_mode={mode}")

        if "admission_deadline" in data:
            raw_deadline = data["admission_deadline"]
            if raw_deadline in (None, ""):
                meeting.admission_deadline = None
            else:
                parsed = parse_datetime(str(raw_deadline))
                if parsed is None:
                    return False, "Invalid admission deadline.", None, None
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(parsed, timezone.utc)
                if meeting.scheduled_at and parsed <= meeting.scheduled_at:
                    return False, "Admission deadline must be after the scheduled start time.", None, None
                meeting.admission_deadline = parsed
            updated_fields.append("admission_deadline")
            changes.append(f"deadline={meeting.admission_deadline}")

        if updated_fields:
            meeting.save(update_fields=list(dict.fromkeys(updated_fields)))
            ActivityLog.objects.create(
                user=teacher,
                action="meeting_permissions_updated",
                detail=f"{meeting.meeting_code}: {', '.join(changes)}",
            )

        perms = _permissions_dict_sync(meeting, is_teacher=False)
        # Drop is_teacher-biased booleans for global broadcast; clients evaluate role locally
        perms.pop("can_draw_whiteboard", None)
        perms.pop("can_clear_whiteboard", None)
        return True, None, perms, revoked_sid


@sync_to_async
def _moderate_participant_db(meeting_code: str, teacher_id: int, data: dict):
    action = str(data.get("action", "")).strip().lower()
    bulk_actions = {"mute_all_mics", "unmute_all_mics", "disable_all_cams", "enable_all_cams"}
    user_actions = {
        "mute_mic",
        "unmute_mic",
        "lock_mic",
        "unlock_mic",
        "disable_cam",
        "enable_cam",
        "mute_chat",
        "unmute_chat",
        "lower_hand",
        "remove",
    }
    if action not in bulk_actions and action not in user_actions:
        return False, "Invalid moderation action.", None

    with transaction.atomic():
        teacher = User.objects.filter(id=teacher_id, is_active=True).first()
        meeting = (
            Meeting.objects.select_for_update()
            .select_related("host", "active_screen_sharer")
            .filter(meeting_code=meeting_code.lower())
            .first()
        )
        if not teacher or not meeting:
            return False, "Meeting not found.", None
        if not is_meeting_teacher(teacher, meeting):
            ActivityLog.objects.create(
                user=teacher,
                action="unauthorized_moderation",
                detail=f"{meeting.meeting_code}: action={action}",
            )
            return False, "Only the meeting teacher can moderate participants.", None

        students_qs = (
            meeting.participants.select_for_update()
            .filter(admission_status=MeetingParticipant.ADMISSION_ADMITTED)
            .exclude(user_id=meeting.host_id)
            .exclude(role__in=MeetingParticipant.TEACHER_ROLES)
        )

        if action in bulk_actions:
            if action == "mute_all_mics":
                meeting.allow_student_mic = False
                meeting.save(update_fields=["allow_student_mic"])
                students_qs.update(mic_locked=True)
            elif action == "unmute_all_mics":
                meeting.allow_student_mic = True
                meeting.save(update_fields=["allow_student_mic"])
                students_qs.update(mic_locked=False)
            elif action == "disable_all_cams":
                meeting.allow_student_camera = False
                meeting.save(update_fields=["allow_student_camera"])
                students_qs.update(camera_locked=True)
            elif action == "enable_all_cams":
                meeting.allow_student_camera = True
                meeting.save(update_fields=["allow_student_camera"])
                students_qs.update(camera_locked=False)

            ActivityLog.objects.create(
                user=teacher,
                action="bulk_moderation",
                detail=f"{meeting.meeting_code}: {action}",
            )
            perms = _permissions_dict_sync(meeting, is_teacher=False)
            perms.pop("can_draw_whiteboard", None)
            perms.pop("can_clear_whiteboard", None)
            return True, None, {"type": "bulk", "action": action, "permissions": perms}

        try:
            target_user_id = int(data.get("user_id"))
        except (TypeError, ValueError):
            return False, "Valid user_id is required.", None

        participant = (
            MeetingParticipant.objects.select_for_update()
            .select_related("user")
            .filter(meeting=meeting, user_id=target_user_id)
            .first()
        )
        if not participant:
            return False, "Participant not found.", None
        if participant.is_teacher:
            return False, "Cannot moderate the meeting teacher.", None

        now = timezone.now()
        if action in ("mute_mic", "lock_mic"):
            participant.mic_locked = True
            participant.save(update_fields=["mic_locked"])
        elif action in ("unmute_mic", "unlock_mic"):
            participant.mic_locked = False
            participant.save(update_fields=["mic_locked"])
        elif action == "disable_cam":
            participant.camera_locked = True
            participant.save(update_fields=["camera_locked"])
        elif action == "enable_cam":
            participant.camera_locked = False
            participant.save(update_fields=["camera_locked"])
        elif action == "mute_chat":
            participant.chat_muted = True
            participant.save(update_fields=["chat_muted"])
        elif action == "unmute_chat":
            participant.chat_muted = False
            participant.save(update_fields=["chat_muted"])
        elif action == "lower_hand":
            participant.hand_raised = False
            participant.save(update_fields=["hand_raised"])
        elif action == "remove":
            participant.admission_status = MeetingParticipant.ADMISSION_REMOVED
            participant.is_active = False
            participant.removed_at = now
            participant.left_at = now
            participant.hand_raised = False
            participant.current_breakout = None
            participant.save(
                update_fields=[
                    "admission_status",
                    "is_active",
                    "removed_at",
                    "left_at",
                    "hand_raised",
                    "current_breakout",
                ]
            )
            close_user_attendance_sessions(meeting, participant.user_id, at_time=now)
            if meeting.active_screen_sharer_id == participant.user_id:
                meeting.active_screen_sharer = None
                meeting.active_screen_sharer_sid = ""
                meeting.save(update_fields=["active_screen_sharer", "active_screen_sharer_sid"])

        ActivityLog.objects.create(
            user=teacher,
            action="participant_moderated",
            detail=f"{meeting.meeting_code}: {action} @{participant.user.username}",
        )
        return (
            True,
            None,
            {
                "type": "user",
                "action": action,
                "target_user_id": participant.user_id,
                "mic_locked": (not meeting.allow_student_mic) or participant.mic_locked,
                "camera_locked": (not meeting.allow_student_camera) or participant.camera_locked,
                "chat_muted": participant.chat_muted,
                "hand_raised": participant.hand_raised,
            },
        )


@sync_to_async
def _breakout_action_db(meeting_code: str, user_id: int, data: dict):
    action = str(data.get("action", "")).strip().lower()
    with transaction.atomic():
        user = User.objects.filter(id=user_id, is_active=True).first()
        meeting = Meeting.objects.select_for_update().select_related("host").filter(meeting_code=meeting_code.lower()).first()
        if not user or not meeting:
            return False, "Meeting not found.", None, {}
        is_teacher = is_meeting_teacher(user, meeting)
        now = timezone.now()
        expired_ids = expire_breakout_rooms(meeting, now)
        if expired_ids:
            return True, None, _serialize_breakout_rooms_sync(meeting), {
                "__expired_to_main__": expired_ids,
            }
        if action == "expire":
            return True, None, _serialize_breakout_rooms_sync(meeting), {
                "__expired_to_main__": [],
            }

        if action == "return_to_main":
            p = (
                meeting.participants.select_for_update()
                .select_related("current_breakout")
                .filter(user=user)
                .first()
            )
            if p and p.current_breakout:
                if not is_teacher and p.current_breakout.status == BreakoutRoom.STATUS_OPEN and not p.current_breakout.allow_return_to_main:
                    return False, "The teacher has locked students inside breakout rooms until they close.", None, {}
                p.current_breakout = None
                p.save(update_fields=["current_breakout"])
            return True, None, _serialize_breakout_rooms_sync(meeting), {user.id: None}

        if action == "switch":
            room_id = data.get("breakout_room_id")
            if room_id in (None, ""):
                if not is_teacher:
                    p = (
                        meeting.participants.select_for_update()
                        .select_related("current_breakout")
                        .filter(user=user)
                        .first()
                    )
                    if p and p.current_breakout and p.current_breakout.status == BreakoutRoom.STATUS_OPEN and not p.current_breakout.allow_return_to_main:
                        return False, "The teacher has locked students inside breakout rooms until they close.", None, {}
                    if p:
                        p.current_breakout = None
                        p.save(update_fields=["current_breakout"])
                return True, None, _serialize_breakout_rooms_sync(meeting), {user.id: None}

            room = BreakoutRoom.objects.filter(id=room_id, meeting=meeting).first()
            if not room or room.status != BreakoutRoom.STATUS_OPEN:
                return False, "Breakout room is not currently open.", None, {}
            if not is_teacher:
                p = meeting.participants.filter(user=user, admission_status=MeetingParticipant.ADMISSION_ADMITTED).first()
                if not p or str(p.current_breakout_id) != str(room.id):
                    return False, "You are not assigned to this breakout room.", None, {}
            return True, None, _serialize_breakout_rooms_sync(meeting), {user.id: str(room.id)}

        if not is_teacher:
            return False, "Only the meeting teacher can manage breakout rooms.", None, {}

        if action == "create":
            name = str(data.get("name", "")).strip()
            count = data.get("count")
            duration_minutes = data.get("duration_minutes")
            allow_return = data.get("allow_return_to_main", True)
            auto_assign = bool(data.get("auto_assign", False))

            if duration_minutes is not None and str(duration_minutes).strip() != "":
                try:
                    duration_minutes = int(duration_minutes)
                    if duration_minutes < 1 or duration_minutes > 240:
                        raise ValueError()
                except (TypeError, ValueError):
                    return False, "Duration must be between 1 and 240 minutes.", None, {}
            else:
                duration_minutes = None

            created_rooms = []
            names_list = data.get("names")
            if isinstance(names_list, list) and names_list:
                for raw_name in names_list[:20]:
                    clean_name = str(raw_name or "").strip()[:80]
                    if clean_name:
                        created_rooms.append(
                            BreakoutRoom.objects.create(
                                meeting=meeting,
                                name=clean_name,
                                duration_minutes=duration_minutes,
                                allow_return_to_main=bool(allow_return),
                                created_by=user,
                            )
                        )
            elif name:
                created_rooms.append(
                    BreakoutRoom.objects.create(
                        meeting=meeting,
                        name=name[:80],
                        duration_minutes=duration_minutes,
                        allow_return_to_main=bool(allow_return),
                        created_by=user,
                    )
                )
            else:
                try:
                    count_int = int(count or 2)
                    if count_int < 1 or count_int > 20:
                        raise ValueError()
                except (TypeError, ValueError):
                    return False, "Room count must be between 1 and 20.", None, {}
                existing_count = meeting.breakout_rooms.exclude(status=BreakoutRoom.STATUS_CLOSED).count()
                for idx in range(existing_count + 1, existing_count + count_int + 1):
                    created_rooms.append(
                        BreakoutRoom.objects.create(
                            meeting=meeting,
                            name=f"Breakout Room {idx}",
                            duration_minutes=duration_minutes,
                            allow_return_to_main=bool(allow_return),
                            created_by=user,
                        )
                    )

            if auto_assign and created_rooms:
                students = list(
                    meeting.participants.select_for_update()
                    .filter(admission_status=MeetingParticipant.ADMISSION_ADMITTED, is_active=True)
                    .exclude(user_id=meeting.host_id)
                    .exclude(role__in=MeetingParticipant.TEACHER_ROLES)
                    .order_by("joined_at")
                )
                for idx, student in enumerate(students):
                    student.current_breakout = created_rooms[idx % len(created_rooms)]
                    student.save(update_fields=["current_breakout"])

            return True, None, _serialize_breakout_rooms_sync(meeting), {}

        if action == "assign":
            raw_ids = data.get("user_ids")
            target_ids = []
            if isinstance(raw_ids, list) and raw_ids:
                try:
                    target_ids = [int(uid) for uid in raw_ids]
                except (TypeError, ValueError):
                    return False, "Valid user_ids are required.", None, {}
            else:
                try:
                    target_ids = [int(data.get("user_id"))]
                except (TypeError, ValueError):
                    return False, "Valid user_id is required.", None, {}

            room_id = data.get("breakout_room_id")
            if room_id in (None, ""):
                room_id = data.get("breakout_id")
            target_room = None
            if room_id not in (None, ""):
                target_room = BreakoutRoom.objects.filter(id=room_id, meeting=meeting).first()
                if not target_room:
                    return False, "Breakout room not found.", None, {}

            user_moves = {}
            for target_user_id in target_ids:
                participant = (
                    meeting.participants.select_for_update()
                    .filter(user_id=target_user_id, admission_status=MeetingParticipant.ADMISSION_ADMITTED)
                    .first()
                )
                if not participant:
                    return False, "Participant not found.", None, {}
                participant.current_breakout = target_room
                participant.save(update_fields=["current_breakout"])
                if target_room is None:
                    user_moves[target_user_id] = None
                elif target_room.status == BreakoutRoom.STATUS_OPEN:
                    user_moves[target_user_id] = str(target_room.id)
            return True, None, _serialize_breakout_rooms_sync(meeting), user_moves

        if action == "set_return_policy":
            allow_ret = bool(data.get("allow_return_to_main", True))
            meeting.breakout_rooms.exclude(status=BreakoutRoom.STATUS_CLOSED).update(
                allow_return_to_main=allow_ret
            )
            return True, None, _serialize_breakout_rooms_sync(meeting), {}

        if action == "auto_assign":
            rooms = list(meeting.breakout_rooms.exclude(status=BreakoutRoom.STATUS_CLOSED))
            if not rooms:
                return False, "Create breakout rooms first.", None, {}
            students = list(
                meeting.participants.select_for_update()
                .filter(admission_status=MeetingParticipant.ADMISSION_ADMITTED, is_active=True)
                .exclude(user_id=meeting.host_id)
                .exclude(role__in=MeetingParticipant.TEACHER_ROLES)
                .order_by("joined_at")
            )
            user_moves = {}
            for idx, student in enumerate(students):
                target_room = rooms[idx % len(rooms)]
                student.current_breakout = target_room
                student.save(update_fields=["current_breakout"])
                if target_room.status == BreakoutRoom.STATUS_OPEN:
                    user_moves[student.user_id] = str(target_room.id)
            return True, None, _serialize_breakout_rooms_sync(meeting), user_moves

        if action == "open_all":
            rooms = list(meeting.breakout_rooms.select_for_update().exclude(status=BreakoutRoom.STATUS_CLOSED))
            if not rooms:
                return False, "No breakout rooms available to open.", None, {}
            dur = data.get("duration_minutes")
            dur_int = None
            ends_at = None
            if dur is not None and str(dur).strip() != "":
                try:
                    dur_int = int(dur)
                    if dur_int < 1 or dur_int > 240:
                        raise ValueError()
                    ends_at = now + timedelta(minutes=dur_int)
                except (TypeError, ValueError):
                    return False, "Duration must be between 1 and 240 minutes.", None, {}
            for room in rooms:
                room.status = BreakoutRoom.STATUS_OPEN
                room.opened_at = now
                room.closed_at = None
                if dur_int is not None:
                    room.duration_minutes = dur_int
                    room.ends_at = ends_at
                elif room.duration_minutes:
                    room.ends_at = now + timedelta(minutes=room.duration_minutes)
                room.save(update_fields=["status", "opened_at", "closed_at", "duration_minutes", "ends_at"])

            user_moves = {}
            for p in meeting.participants.filter(
                admission_status=MeetingParticipant.ADMISSION_ADMITTED,
                current_breakout__in=rooms,
            ):
                user_moves[p.user_id] = str(p.current_breakout_id)
            ActivityLog.objects.create(
                user=user,
                action="breakouts_opened",
                detail=f"{meeting.meeting_code}: {len(rooms)} rooms",
            )
            return True, None, _serialize_breakout_rooms_sync(meeting), user_moves

        if action == "close_all":
            meeting.breakout_rooms.filter(status=BreakoutRoom.STATUS_OPEN).update(
                status=BreakoutRoom.STATUS_CLOSED,
                closed_at=now,
            )
            meeting.participants.update(current_breakout=None)
            ActivityLog.objects.create(
                user=user,
                action="breakouts_closed",
                detail=meeting.meeting_code,
            )
            return True, None, _serialize_breakout_rooms_sync(meeting), {"__all_to_main__": True}

        if action in ("broadcast", "broadcast_message"):
            text = str(data.get("message") or data.get("text") or "").strip()[:500]
            if not text:
                return False, "Broadcast message cannot be empty.", None, {}
            return True, None, _serialize_breakout_rooms_sync(meeting), {"__broadcast__": text}

        return False, "Unsupported breakout action.", None, {}


@sync_to_async
def _set_recording_state_db(meeting_code: str, user_id: int, is_recording: bool):
    with transaction.atomic():
        user = User.objects.filter(id=user_id, is_active=True).first()
        meeting = Meeting.objects.select_for_update().filter(meeting_code=meeting_code.lower()).first()
        if not user or not meeting:
            return False, "Meeting not found.", None
        if not is_meeting_teacher(user, meeting):
            ActivityLog.objects.create(
                user=user,
                action="unauthorized_recording_control",
                detail=meeting.meeting_code,
            )
            return False, "Only the meeting teacher can control session recording.", None
        if meeting.status == Meeting.STATUS_ENDED:
            return False, "Cannot record an ended meeting.", None
        now = timezone.now()
        if is_recording:
            meeting.is_recording = True
            meeting.recording_started_by = user
            meeting.recording_started_at = now
        else:
            meeting.is_recording = False
            meeting.recording_started_by = None
            meeting.recording_started_at = None
        meeting.save(update_fields=["is_recording", "recording_started_by", "recording_started_at"])
        ActivityLog.objects.create(
            user=user,
            action="recording_started" if is_recording else "recording_stopped",
            detail=meeting.meeting_code,
        )
        return True, None, {
            "meeting_code": meeting.meeting_code,
            "is_recording": meeting.is_recording,
            "started_by": user.username if is_recording else None,
            "started_at": meeting.recording_started_at.isoformat() if meeting.recording_started_at else None,
        }


@sync_to_async
def _release_screen_share_on_leave(meeting_code: str, sid: str):
    with transaction.atomic():
        meeting = Meeting.objects.select_for_update().filter(meeting_code=meeting_code.lower()).first()
        if not meeting or meeting.active_screen_sharer_sid != sid:
            return None
        meeting.active_screen_sharer = None
        meeting.active_screen_sharer_sid = ""
        meeting.save(update_fields=["active_screen_sharer", "active_screen_sharer_sid"])
        return {
            "meeting_code": meeting.meeting_code,
            "active_screen_sharer_id": None,
            "active_screen_sharer_username": None,
            "active_screen_sharer_sid": "",
        }


@sync_to_async
def _mark_meeting_ended(meeting_code: str, user_id: int):
    with transaction.atomic():
        user = User.objects.filter(id=user_id, is_active=True).first()
        meeting = Meeting.objects.select_for_update().filter(meeting_code=meeting_code.lower()).first()
        if not user or not meeting or not is_meeting_teacher(user, meeting):
            return False
        now = timezone.now()
        if meeting.status != Meeting.STATUS_ENDED:
            meeting.status = Meeting.STATUS_ENDED
            meeting.ended_at = now
            meeting.active_screen_sharer = None
            meeting.active_screen_sharer_sid = ""
            meeting.is_recording = False
            meeting.recording_started_by = None
            meeting.recording_started_at = None
            meeting.save(
                update_fields=[
                    "status",
                    "ended_at",
                    "active_screen_sharer",
                    "active_screen_sharer_sid",
                    "is_recording",
                    "recording_started_by",
                    "recording_started_at",
                ]
            )
            MeetingParticipant.objects.filter(meeting=meeting, is_active=True).update(
                is_active=False, left_at=now, hand_raised=False, current_breakout=None
            )
            MeetingParticipant.objects.filter(meeting=meeting).update(current_breakout=None, hand_raised=False)
            BreakoutRoom.objects.filter(meeting=meeting).exclude(status=BreakoutRoom.STATUS_CLOSED).update(
                status=BreakoutRoom.STATUS_CLOSED,
                closed_at=now,
            )
            close_all_attendance_sessions(meeting, at_time=now)
            ActivityLog.objects.create(
                user=user,
                action="meeting_ended",
                detail=meeting.meeting_code,
            )
        return True


@sync_to_async
def _verify_file_in_meeting(file_id: str, meeting_code: str) -> bool:
    if not file_id:
        return False
    return SharedFile.objects.filter(id=file_id, meeting__meeting_code=meeting_code).exists()


def _teacher_sids_in_room(code: str) -> list[str]:
    return [
        peer_sid
        for peer_sid, info in ROOM_PEERS.get(code, {}).items()
        if info.get("is_host") or info.get("meeting_role") == MeetingParticipant.ROLE_TEACHER
    ]


def _host_sids_in_room(code: str) -> list[str]:
    return [
        peer_sid
        for peer_sid, info in ROOM_PEERS.get(code, {}).items()
        if info.get("is_host") is True
    ]


async def _emit_to_host(code: str, event: str, payload: dict):
    for host_sid in _host_sids_in_room(code):
        await sio.emit(event, payload, to=host_sid)




async def _emit_to_teachers(code: str, event: str, payload: dict):
    for t_sid in _teacher_sids_in_room(code):
        await sio.emit(event, payload, to=t_sid)


async def _leave_all_rooms(sid: str):
    session = await sio.get_session(sid)
    if not session:
        return
    rooms = list(session.get("rooms", set()))
    for code in rooms:
        await _remove_sid_from_room(sid, code)
    waiting_rooms = list(session.get("waiting_rooms", set()))
    for code in waiting_rooms:
        await _remove_sid_from_waiting(sid, code)
    session["rooms"] = set()
    session["waiting_rooms"] = set()
    await sio.save_session(sid, session)


async def _remove_sid_from_room(sid: str, code: str):
    peers = ROOM_PEERS.get(code)
    departed = peers.pop(sid, None) if peers else None
    departed_breakout_id = departed.get("breakout_room_id") if departed else None
    subchannel = room_subchannel(code, departed_breakout_id)
    await sio.leave_room(sid, subchannel)
    await sio.leave_room(sid, control_channel(code))
    await sio.leave_room(sid, room_channel(code))

    if peers is not None and not peers:
        ROOM_PEERS.pop(code, None)

    if departed:
        released = await _release_screen_share_on_leave(code, sid)
        if released:
            await sio.emit("screen_share_state", released, room=subchannel, skip_sid=sid)
        await sio.emit(
            "participant_left",
            {"sid": sid, "username": departed.get("username")},
            room=subchannel,
            skip_sid=sid,
        )
        departed_uid = departed.get("user_id")
        still_connected = any(
            p.get("user_id") == departed_uid for p in ROOM_PEERS.get(code, {}).values()
        )
        if departed_uid and not still_connected:
            await _close_disconnected_attendance(code, departed_uid)
            await _emit_to_teachers(code, "attendance_updated", {"meeting_code": code})


async def _remove_sid_from_waiting(sid: str, code: str):
    await sio.leave_room(sid, waiting_channel(code))
    waiting = WAITING_PEERS.get(code)
    if waiting and sid in waiting:
        waiting.pop(sid, None)
        if not waiting:
            WAITING_PEERS.pop(code, None)


async def _move_peer_subchannel(code: str, target_sid: str, new_breakout_id):
    peers = ROOM_PEERS.get(code, {})
    peer = peers.get(target_sid)
    if not peer:
        return
    old_breakout_id = peer.get("breakout_room_id") or None
    norm_new_id = str(new_breakout_id) if new_breakout_id else None
    if old_breakout_id == norm_new_id:
        return

    old_channel = room_subchannel(code, old_breakout_id)
    new_channel = room_subchannel(code, norm_new_id)

    if peer.get("screen_sharing"):
        released = await _release_screen_share_on_leave(code, target_sid)
        peer["screen_sharing"] = False
        if released:
            await sio.emit("screen_share_state", released, room=old_channel, skip_sid=target_sid)
        await sio.emit(
            "screen_share_revoked",
            {"meeting_code": code, "message": "Screen share stopped when switching rooms."},
            to=target_sid,
        )

    await sio.emit(
        "participant_left",
        {"sid": target_sid, "username": peer.get("username"), "reason": "breakout_move"},
        room=old_channel,
        skip_sid=target_sid,
    )
    await sio.leave_room(target_sid, old_channel)

    peer["breakout_room_id"] = norm_new_id
    await sio.enter_room(target_sid, new_channel)

    wb_ops, chat_messages, permissions, breakout_rooms, room_dict = await _load_subchannel_state(
        code, peer["user_id"], norm_new_id
    )
    existing_peers = [
        p
        for psid, p in peers.items()
        if psid != target_sid and (p.get("breakout_room_id") or None) == norm_new_id
    ]
    move_payload = {
        "meeting_code": code,
        "breakout_room_id": norm_new_id,
        "breakout_room": room_dict,
        "peers": existing_peers,
        "whiteboard_ops": wb_ops,
        "chat_messages": chat_messages,
        "permissions": permissions,
        "breakout_rooms": breakout_rooms,
    }
    event_name = "breakout_entered" if norm_new_id else "breakout_exited"
    await sio.emit(event_name, move_payload, to=target_sid)
    await sio.emit("participant_joined", {"participant": peer}, room=new_channel, skip_sid=target_sid)


@sio.event
async def join_waiting_room(sid, data):
    session = await sio.get_session(sid)
    if not session:
        return
    user_data = session["user"]
    code = ((data or {}).get("meeting_code") or "").strip().lower()

    meeting, admission_status, err = await get_waiting_meeting_context(user_data["user_id"], code)
    if err:
        if admission_status == MeetingParticipant.ADMISSION_REJECTED:
            await sio.emit("admission_rejected", {"meeting_code": code, "detail": err}, to=sid)
        elif admission_status == MeetingParticipant.ADMISSION_REMOVED:
            await sio.emit("participant_removed", {"meeting_code": code, "message": err}, to=sid)
        else:
            await sio.emit("error_event", {"message": err}, to=sid)
        return

    if admission_status == MeetingParticipant.ADMISSION_ADMITTED:
        await sio.emit("admission_approved", {"meeting_code": code}, to=sid)
        return

    w_chan = waiting_channel(code)
    await sio.enter_room(sid, w_chan)
    session.setdefault("waiting_rooms", set()).add(code)
    await sio.save_session(sid, session)

    already_connected_for_user = any(
        entry.get("user_id") == user_data["user_id"]
        for entry in WAITING_PEERS.get(code, {}).values()
    )
    WAITING_PEERS.setdefault(code, {})[sid] = {
        "sid": sid,
        "user_id": user_data["user_id"],
        "username": user_data["username"],
        "full_name": user_data["full_name"],
    }

    waiting_list = await _get_waiting_room_list(code)
    await sio.emit(
        "waiting_room_joined",
        {"meeting_code": code, "admission_status": MeetingParticipant.ADMISSION_WAITING},
        to=sid,
    )
    await _emit_to_host(
        code,
        "waiting_room_updated",
        {"meeting_code": code, "waiting_room": waiting_list},
    )
    if not already_connected_for_user:
        display_name = user_data.get("full_name") or user_data["username"]
        await _emit_to_host(
            code,
            "admission_requested",
            {
                "meeting_code": code,
                "user_id": user_data["user_id"],
                "username": user_data["username"],
                "full_name": display_name,
                "message": f"{display_name} is requesting to join the meeting.",            },
        )


@sio.event
async def leave_waiting_room(sid, data):
    code = ((data or {}).get("meeting_code") or "").strip().lower()
    session = await sio.get_session(sid)
    if not session or not code:
        return
    if code in session.get("waiting_rooms", set()):
        session["waiting_rooms"].discard(code)
        await sio.save_session(sid, session)
        await _remove_sid_from_waiting(sid, code)


@sio.event
async def admission_decision(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        await sio.emit(
            "error_event",
            {"message": "Unauthorized request."},
            to=sid,
        )
        return

    # Verify user is host
    meeting = await sync_to_async(Meeting.objects.get)(meeting_code=code)
    if meeting.host_id != user_data["user_id"]:
        await sio.emit(
            "error_event",
            {"message": "Only the Host can admit or reject students."},
            to=sid,
        )
        return

    decision = str(data.get("decision", "")).strip().lower()
    if decision not in ("admit", "reject"):
        await sio.emit("error_event", {"message": "Invalid admission decision."}, to=sid)
        return
    try:
        target_user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        await sio.emit("error_event", {"message": "Valid user_id is required."}, to=sid)
        return

    async with get_room_lock(code):
        ok, result, waiting_list = await _process_admission_decision(
            code, user_data["user_id"], target_user_id, decision
        )
        if not ok:
            await sio.emit("error_event", {"message": result}, to=sid)
            return

        target_sids = [
            w_sid
            for w_sid, w_info in list(WAITING_PEERS.get(code, {}).items())
            if w_info.get("user_id") == target_user_id
        ]
        for target_sid in target_sids:
            if decision == "admit":
                await sio.emit("admission_approved", {"meeting_code": code}, to=target_sid)
            else:
                await sio.emit(
                    "admission_rejected",
                    {
                        "meeting_code": code,
                        "detail": "Your admission request was declined by the Host.",
                    },
                    to=target_sid,
                )
            await _remove_sid_from_waiting(target_sid, code)
            target_session = await sio.get_session(target_sid)
            if target_session and code in target_session.get("waiting_rooms", set()):
                target_session["waiting_rooms"].discard(code)
                await sio.save_session(target_sid, target_session)

        await _emit_to_host(code, "waiting_room_updated", {"meeting_code": code, "waiting_room": waiting_list})


@sio.event
async def update_meeting_permissions(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_teacher_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        await sio.emit(
            "error_event",
            {"message": "Only the meeting teacher can update meeting permissions."},
            to=sid,
        )
        return

    async with get_room_lock(code):
        ok, err, perms, revoked_sid = await _update_permissions_db(code, user_data["user_id"], data)
        if not ok:
            await sio.emit("error_event", {"message": err or "Permission update failed."}, to=sid)
            return

        if revoked_sid and revoked_sid in ROOM_PEERS.get(code, {}):
            ROOM_PEERS[code][revoked_sid]["screen_sharing"] = False
            revoked_channel = get_peer_subchannel(code, revoked_sid)
            await sio.emit(
                "screen_share_revoked",
                {"meeting_code": code, "message": "Screen sharing was disabled by the teacher."},
                to=revoked_sid,
            )
            await sio.emit(
                "participant_media_state",
                {
                    "sid": revoked_sid,
                    "mic_enabled": ROOM_PEERS[code][revoked_sid].get("mic_enabled", True),
                    "cam_enabled": ROOM_PEERS[code][revoked_sid].get("cam_enabled", True),
                    "screen_sharing": False,
                },
                room=revoked_channel,
            )
            await sio.emit(
                "screen_share_state",
                {
                    "meeting_code": code,
                    "active_screen_sharer_id": None,
                    "active_screen_sharer_username": None,
                    "active_screen_sharer_sid": "",
                },
                room=revoked_channel,
            )

        # Enforce global mic/cam lock across connected student peers when toggled off
        if "allow_student_mic" in data or "allow_student_camera" in data:
            allow_mic_global = perms.get("allow_student_mic", True)
            allow_cam_global = perms.get("allow_student_camera", True)
            for p_sid, p_info in list(ROOM_PEERS.get(code, {}).items()):
                if p_info.get("is_host") or p_info.get("meeting_role") == MeetingParticipant.ROLE_TEACHER:
                    continue
                changed = False
                if not allow_mic_global:
                    p_info["mic_enabled"] = False
                    p_info["mic_locked"] = True
                    changed = True
                elif "allow_student_mic" in data and allow_mic_global:
                    p_info["mic_locked"] = False
                if not allow_cam_global:
                    p_info["cam_enabled"] = False
                    p_info["camera_locked"] = True
                    changed = True
                elif "allow_student_camera" in data and allow_cam_global:
                    p_info["camera_locked"] = False
                if changed or "allow_student_mic" in data or "allow_student_camera" in data:
                    await sio.emit(
                        "moderation_enforced",
                        {
                            "meeting_code": code,
                            "mic_enabled": p_info["mic_enabled"],
                            "cam_enabled": p_info["cam_enabled"],
                            "mic_locked": p_info.get("mic_locked", False),
                            "camera_locked": p_info.get("camera_locked", False),
                        },
                        to=p_sid,
                    )
                    await sio.emit(
                        "participant_media_state",
                        {
                            "sid": p_sid,
                            "mic_enabled": p_info["mic_enabled"],
                            "cam_enabled": p_info["cam_enabled"],
                            "screen_sharing": p_info.get("screen_sharing", False),
                            "mic_locked": p_info.get("mic_locked", False),
                            "camera_locked": p_info.get("camera_locked", False),
                        },
                        room=get_peer_subchannel(code, p_sid),
                    )

        await sio.emit(
            "meeting_permissions_updated",
            {"meeting_code": code, "permissions": perms},
            room=control_channel(code),
        )


@sio.event
async def moderate_participant(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_teacher_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        await sio.emit(
            "error_event",
            {"message": "Only the meeting teacher can moderate participants."},
            to=sid,
        )
        return

    async with get_room_lock(code):
        ok, err, result = await _moderate_participant_db(code, user_data["user_id"], data)
        if not ok:
            await sio.emit("error_event", {"message": err or "Moderation failed."}, to=sid)
            return

        if result["type"] == "bulk":
            perms = result["permissions"]
            action = result["action"]
            for p_sid, p_info in list(ROOM_PEERS.get(code, {}).items()):
                if p_info.get("is_host") or p_info.get("meeting_role") == MeetingParticipant.ROLE_TEACHER:
                    continue
                if action == "mute_all_mics":
                    p_info["mic_enabled"] = False
                    p_info["mic_locked"] = True
                elif action == "unmute_all_mics":
                    p_info["mic_locked"] = False
                elif action == "disable_all_cams":
                    p_info["cam_enabled"] = False
                    p_info["camera_locked"] = True
                elif action == "enable_all_cams":
                    p_info["camera_locked"] = False

                await sio.emit(
                    "moderation_enforced",
                    {
                        "meeting_code": code,
                        "action": action,
                        "mic_enabled": p_info["mic_enabled"],
                        "cam_enabled": p_info["cam_enabled"],
                        "mic_locked": p_info.get("mic_locked", False),
                        "camera_locked": p_info.get("camera_locked", False),
                    },
                    to=p_sid,
                )
                await sio.emit(
                    "participant_media_state",
                    {
                        "sid": p_sid,
                        "mic_enabled": p_info["mic_enabled"],
                        "cam_enabled": p_info["cam_enabled"],
                        "screen_sharing": p_info.get("screen_sharing", False),
                        "mic_locked": p_info.get("mic_locked", False),
                        "camera_locked": p_info.get("camera_locked", False),
                    },
                    room=get_peer_subchannel(code, p_sid),
                )
            await sio.emit(
                "meeting_permissions_updated",
                {"meeting_code": code, "permissions": perms},
                room=control_channel(code),
            )
            return

        # Single participant moderation
        action = result["action"]
        target_uid = result["target_user_id"]
        target_sids = [
            psid for psid, pinfo in list(ROOM_PEERS.get(code, {}).items()) if pinfo.get("user_id") == target_uid
        ]
        if action == "remove":
            for t_sid in target_sids:
                await sio.emit(
                    "participant_removed",
                    {
                        "meeting_code": code,
                        "message": "You have been removed from this meeting by the teacher.",
                    },
                    to=t_sid,
                )
                t_session = await sio.get_session(t_sid)
                if t_session and code in t_session.get("rooms", set()):
                    t_session["rooms"].discard(code)
                    await sio.save_session(t_sid, t_session)
                await _remove_sid_from_room(t_sid, code)
            await _emit_to_teachers(code, "attendance_updated", {"meeting_code": code})
            return

        for t_sid in target_sids:
            p_info = ROOM_PEERS.get(code, {}).get(t_sid)
            if not p_info:
                continue
            p_info["mic_locked"] = result["mic_locked"]
            p_info["camera_locked"] = result["camera_locked"]
            p_info["chat_muted"] = result["chat_muted"]
            p_info["hand_raised"] = result["hand_raised"]
            if result["mic_locked"]:
                p_info["mic_enabled"] = False
            if result["camera_locked"]:
                p_info["cam_enabled"] = False

            await sio.emit(
                "moderation_enforced",
                {
                    "meeting_code": code,
                    "action": action,
                    "mic_enabled": p_info["mic_enabled"],
                    "cam_enabled": p_info["cam_enabled"],
                    "mic_locked": p_info["mic_locked"],
                    "camera_locked": p_info["camera_locked"],
                    "chat_muted": p_info["chat_muted"],
                    "hand_raised": p_info["hand_raised"],
                },
                to=t_sid,
            )
            await sio.emit(
                "participant_media_state",
                {
                    "sid": t_sid,
                    "user_id": target_uid,
                    "mic_enabled": p_info["mic_enabled"],
                    "cam_enabled": p_info["cam_enabled"],
                    "screen_sharing": p_info.get("screen_sharing", False),
                    "mic_locked": p_info["mic_locked"],
                    "camera_locked": p_info["camera_locked"],
                    "chat_muted": p_info["chat_muted"],
                },
                room=get_peer_subchannel(code, t_sid),
            )
            if action == "lower_hand":
                await sio.emit(
                    "participant_hand_state",
                    {
                        "meeting_code": code,
                        "sid": t_sid,
                        "user_id": target_uid,
                        "username": p_info.get("username"),
                        "hand_raised": False,
                    },
                    room=control_channel(code),
                )


@sio.event
async def breakout_action(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        await sio.emit("error_event", {"message": "Not authorized in this meeting."}, to=sid)
        return

    async with get_room_lock(code):
        ok, err, rooms_data, moves = await _breakout_action_db(code, user_data["user_id"], data)
        if not ok:
            await sio.emit("error_event", {"message": err or "Breakout action failed."}, to=sid)
            return

        if "__broadcast__" in moves:
            await sio.emit(
                "breakout_announcement",
                {
                    "meeting_code": code,
                    "message": moves["__broadcast__"],
                    "sender": user_data.get("full_name") or user_data["username"],
                },
                room=control_channel(code),
            )
            return

        if "__all_to_main__" in moves:
            for psid in list(ROOM_PEERS.get(code, {}).keys()):
                await _move_peer_subchannel(code, psid, None)
        elif "__expired_to_main__" in moves:
            expired_ids = set(moves["__expired_to_main__"])
            if expired_ids:
                for psid, peer in list(ROOM_PEERS.get(code, {}).items()):
                    if peer.get("breakout_room_id") in expired_ids:
                        await _move_peer_subchannel(code, psid, None)
        else:
            for target_uid, dest_room_id in moves.items():
                matching_sids = [
                    psid
                    for psid, pinfo in list(ROOM_PEERS.get(code, {}).items())
                    if pinfo.get("user_id") == target_uid
                ]
                for psid in matching_sids:
                    await _move_peer_subchannel(code, psid, dest_room_id)

        await sio.emit(
            "breakout_state_updated",
            {"meeting_code": code, "breakout_rooms": rooms_data},
            room=control_channel(code),
        )


@sio.event
async def switch_breakout_room(sid, data):
    data = dict(data) if isinstance(data, dict) else {}
    data["action"] = "switch"
    await breakout_action(sid, data)


@sio.event
async def start_recording(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_teacher_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        await sio.emit(
            "error_event",
            {"message": "Only the meeting teacher can start recording."},
            to=sid,
        )
        return
    async with get_room_lock(code):
        ok, err, payload = await _set_recording_state_db(code, user_data["user_id"], True)
        if not ok:
            await sio.emit("error_event", {"message": err}, to=sid)
            return
        await sio.emit("recording_state_changed", payload, room=control_channel(code))


@sio.event
async def stop_recording(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_teacher_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        await sio.emit(
            "error_event",
            {"message": "Only the meeting teacher can stop recording."},
            to=sid,
        )
        return
    async with get_room_lock(code):
        ok, err, payload = await _set_recording_state_db(code, user_data["user_id"], False)
        if not ok:
            await sio.emit("error_event", {"message": err}, to=sid)
            return
        await sio.emit("recording_state_changed", payload, room=control_channel(code))


@sio.event
async def join_meeting(sid, data):
    session = await sio.get_session(sid)
    if not session:
        return
    user_data = session["user"]
    code = ((data or {}).get("meeting_code") or "").strip().lower()

    meeting, is_teacher, err = await get_authorized_meeting_context(user_data["user_id"], code)
    if err or not meeting:
        await sio.emit("error_event", {"message": err or "Unable to join room."}, to=sid)
        return

    is_host = meeting.host_id == user_data["user_id"] if meeting else False

    if code in session.get("waiting_rooms", set()):
        session["waiting_rooms"].discard(code)
        await _remove_sid_from_waiting(sid, code)

    (
        wb_ops,
        chat_messages,
        waiting_room,
        permissions,
        my_moderation,
        breakout_rooms,
        breakout_room_id,
    ) = await _enter_meeting_and_load_state(meeting.id, user_data["user_id"], is_teacher)

    subchannel = room_subchannel(code, breakout_room_id)
    await sio.enter_room(sid, control_channel(code))
    await sio.enter_room(sid, subchannel)
    session.setdefault("rooms", set()).add(code)
    await sio.save_session(sid, session)

    meeting_role = MeetingParticipant.ROLE_TEACHER if is_teacher else MeetingParticipant.ROLE_STUDENT
    peer_info = {
        "sid": sid,
        "user_id": user_data["user_id"],
        "username": user_data["username"],
        "full_name": user_data["full_name"],
        "is_host": is_host,
        "meeting_role": meeting_role,
        "mic_enabled": not my_moderation["mic_locked"],
        "cam_enabled": False,
        "mic_locked": my_moderation["mic_locked"],
        "camera_locked": my_moderation["camera_locked"],
        "chat_muted": my_moderation["chat_muted"],
        "hand_raised": my_moderation["hand_raised"],
        "breakout_room_id": breakout_room_id,
        "screen_sharing": meeting.active_screen_sharer_sid == sid,
    }
    ROOM_PEERS.setdefault(code, {})[sid] = peer_info

    existing_peers = [
        p
        for peer_sid, p in ROOM_PEERS[code].items()
        if peer_sid != sid and (p.get("breakout_room_id") or None) == breakout_room_id
    ]

    payload = {
        "self_sid": sid,
        "meeting_code": code,
        "meeting_role": meeting_role,
        "is_host": is_host,
        "permissions": permissions,
        "my_moderation": my_moderation,
        "breakout_room_id": breakout_room_id,
        "breakout_rooms": breakout_rooms,
        "peers": existing_peers,
        "whiteboard_ops": wb_ops,
        "chat_messages": chat_messages,
    }
    if is_teacher:
        payload["waiting_room"] = waiting_room

    await sio.emit("room_joined", payload, to=sid)
    await sio.emit("participant_joined", {"participant": peer_info}, room=subchannel, skip_sid=sid)
    await _emit_to_teachers(code, "attendance_updated", {"meeting_code": code})
    logger.info(
        "User %s (%s) joined room %s (breakout=%s sid=%s)",
        user_data["username"],
        meeting_role,
        code,
        breakout_room_id,
        sid,
    )


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
    code, user_data = await verify_teacher_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        await sio.emit("error_event", {"message": "Only the teacher can end this meeting."}, to=sid)
        return
    ok = await _mark_meeting_ended(code, user_data["user_id"])
    if not ok:
        await sio.emit("error_event", {"message": "Only the teacher can end this meeting."}, to=sid)
        return

    w_channel = waiting_channel(code)
    await sio.emit("meeting_ended", {"meeting_code": code}, room=control_channel(code))
    await sio.emit("meeting_ended", {"meeting_code": code}, room=room_channel(code))
    await sio.emit("meeting_ended", {"meeting_code": code}, room=w_channel)
    ROOM_PEERS.pop(code, None)
    WAITING_PEERS.pop(code, None)


@sio.event
async def file_shared(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    file_obj = (data or {}).get("file") or {}
    if await _verify_file_in_meeting(str(file_obj.get("id", "")), code):
        await sio.emit("file_shared", {"file": file_obj}, room=control_channel(code), skip_sid=sid)


@sio.event
async def disconnect(sid):
    await _leave_all_rooms(sid)
    logger.info("Socket disconnected sid=%s", sid)
