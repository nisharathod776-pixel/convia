import os
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import AttendanceSession, BreakoutRoom, Meeting, MeetingParticipant
from .permissions import is_meeting_teacher, is_platform_admin

ALLOWED_RECORDING_EXTENSIONS = {".webm", ".mp4", ".mkv", ".ogg"}
ALLOWED_RECORDING_MIMES = {
    "video/webm",
    "audio/webm",
    "video/mp4",
    "video/x-matroska",
    "video/ogg",
    "application/octet-stream",
}


def open_attendance_session(meeting: Meeting, user, role: str, at_time=None):
    now = at_time or timezone.now()
    existing = AttendanceSession.objects.filter(
        meeting=meeting,
        user=user,
        left_at__isnull=True,
    ).first()
    if existing:
        return existing
    return AttendanceSession.objects.create(
        meeting=meeting,
        user=user,
        role=role,
        joined_at=now,
    )


def close_user_attendance_sessions(meeting: Meeting, user_id: int, at_time=None):
    now = at_time or timezone.now()
    open_sessions = AttendanceSession.objects.filter(
        meeting=meeting,
        user_id=user_id,
        left_at__isnull=True,
    )
    for session in open_sessions:
        session.close(at_time=now)


def expire_breakout_rooms(meeting: Meeting, at_time=None):
    now = at_time or timezone.now()
    with transaction.atomic():
        expired_ids = list(
            BreakoutRoom.objects.select_for_update()
            .filter(
                meeting=meeting,
                status=BreakoutRoom.STATUS_OPEN,
                ends_at__lte=now,
            )
            .values_list("id", flat=True)
        )
        transitioned_ids = []
        for room_id in expired_ids:
            updated = BreakoutRoom.objects.filter(
                id=room_id,
                status=BreakoutRoom.STATUS_OPEN,
                ends_at__lte=now,
            ).update(status=BreakoutRoom.STATUS_CLOSED, closed_at=now)
            if updated:
                transitioned_ids.append(room_id)

        if transitioned_ids:
            MeetingParticipant.objects.filter(
                meeting=meeting,
                current_breakout_id__in=transitioned_ids,
            ).update(current_breakout=None)

    return [str(room_id) for room_id in transitioned_ids]


def close_all_attendance_sessions(meeting: Meeting, at_time=None):
    now = at_time or timezone.now()
    open_sessions = AttendanceSession.objects.filter(meeting=meeting, left_at__isnull=True)
    for session in open_sessions:
        session.close(at_time=now)


def format_duration_hms(seconds: int) -> str:
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def csv_safe_cell(value) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def build_attendance_report(meeting: Meeting, viewer_user):
    now = timezone.now()
    can_view_all = is_meeting_teacher(viewer_user, meeting) or is_platform_admin(viewer_user)

    participants_qs = meeting.participants.select_related("user").order_by("joined_at")
    if not can_view_all:
        participants_qs = participants_qs.filter(user=viewer_user)

    sessions_qs = AttendanceSession.objects.filter(meeting=meeting).order_by("joined_at")
    if not can_view_all:
        sessions_qs = sessions_qs.filter(user=viewer_user)

    sessions_by_user = {}
    for sess in sessions_qs:
        sessions_by_user.setdefault(sess.user_id, []).append(sess)

    records = []
    counts = {
        "total": 0,
        "present": 0,
        "late": 0,
        "excused": 0,
        "waiting": 0,
        "rejected": 0,
        "removed": 0,
        "absent": 0,
    }

    late_cutoff = None
    if meeting.scheduled_at:
        from datetime import timedelta

        late_cutoff = meeting.scheduled_at + timedelta(minutes=5)

    for p in participants_qs:
        user_sessions = sessions_by_user.get(p.user_id, [])
        total_seconds = 0
        first_joined = None
        last_left = None
        currently_connected = False

        for s in user_sessions:
            if first_joined is None or s.joined_at < first_joined:
                first_joined = s.joined_at
            if s.left_at is None:
                currently_connected = True
                end_ref = meeting.ended_at or now
                total_seconds += max(0, int((end_ref - s.joined_at).total_seconds()))
            else:
                total_seconds += s.duration_seconds
                if last_left is None or s.left_at > last_left:
                    last_left = s.left_at

        if p.attendance_status_override:
            computed_status = p.attendance_status_override
        elif p.admission_status == MeetingParticipant.ADMISSION_WAITING:
            computed_status = "waiting"
        elif p.admission_status == MeetingParticipant.ADMISSION_REJECTED:
            computed_status = "rejected"
        elif p.admission_status == MeetingParticipant.ADMISSION_REMOVED:
            computed_status = "removed"
        elif first_joined is not None or p.admitted_at is not None:
            join_ref = first_joined or p.admitted_at or p.joined_at
            if not p.is_teacher and late_cutoff and join_ref and join_ref > late_cutoff:
                computed_status = "late"
            else:
                computed_status = "present"
        else:
            computed_status = "absent"

        counts["total"] += 1
        if computed_status in counts:
            counts[computed_status] += 1

        records.append(
            {
                "user_id": p.user_id,
                "username": p.user.username,
                "full_name": p.user.full_name or p.user.username,
                "email": p.user.email,
                "meeting_role": p.normalized_role,
                "admission_status": p.admission_status,
                "is_connected": currently_connected and p.is_active,
                "first_joined_at": first_joined.isoformat() if first_joined else None,
                "last_left_at": last_left.isoformat() if last_left else None,
                "join_count": len(user_sessions),
                "total_duration_seconds": total_seconds,
                "duration_formatted": format_duration_hms(total_seconds),
                "status": computed_status,
                "status_override": p.attendance_status_override,
                "note": p.attendance_note,
            }
        )

    return {
        "meeting_code": meeting.meeting_code,
        "title": meeting.title,
        "meeting_status": meeting.status,
        "scheduled_at": meeting.scheduled_at.isoformat() if meeting.scheduled_at else None,
        "is_teacher": is_meeting_teacher(viewer_user, meeting),
        "can_export": can_view_all,
        "summary": counts,
        "records": records,
    }


def validate_recording_upload(uploaded_file):
    if not uploaded_file:
        raise ValidationError({"file": "No recording file was provided."})

    if uploaded_file.size > settings.MAX_RECORDING_SIZE_BYTES:
        raise ValidationError(
            {"file": f"Recording exceeds the {settings.MAX_RECORDING_SIZE_MB} MB limit."}
        )
    if uploaded_file.size == 0:
        raise ValidationError({"file": "Recording file cannot be empty."})

    original_name = os.path.basename(uploaded_file.name or "recording.webm")
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_RECORDING_EXTENSIONS:
        raise ValidationError(
            {"file": f"Recording format '{ext or 'unknown'}' is not supported. Use WebM or MP4."}
        )

    mime_type = (getattr(uploaded_file, "content_type", "") or "video/webm").split(";")[0].strip().lower()
    if mime_type not in ALLOWED_RECORDING_MIMES:
        raise ValidationError({"file": f"Unsupported recording MIME type '{mime_type}'."})

    return original_name, mime_type
