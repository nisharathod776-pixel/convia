from .models import BreakoutRoom, Meeting, MeetingParticipant, MeetingRecording


def is_platform_admin(user):
    return bool(user and user.is_authenticated and user.is_active and (user.is_staff or user.role == "admin"))


def is_meeting_teacher(user, meeting):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if meeting.host_id == user.id:
        return True
    return MeetingParticipant.objects.filter(
        meeting=meeting,
        user=user,
        role__in=MeetingParticipant.TEACHER_ROLES,
        admission_status=MeetingParticipant.ADMISSION_ADMITTED,
    ).exists()


def get_meeting_role(user, meeting):
    return MeetingParticipant.ROLE_TEACHER if is_meeting_teacher(user, meeting) else MeetingParticipant.ROLE_STUDENT


def is_meeting_member(user, meeting):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if meeting.host_id == user.id:
        return True
    return MeetingParticipant.objects.filter(
        meeting=meeting,
        user=user,
        admission_status=MeetingParticipant.ADMISSION_ADMITTED,
    ).exists()


def can_edit_whiteboard(user, meeting, breakout_room=None):
    if is_meeting_teacher(user, meeting):
        return True
    if not is_meeting_member(user, meeting):
        return False
    if breakout_room is not None and breakout_room.status == BreakoutRoom.STATUS_OPEN:
        return MeetingParticipant.objects.filter(
            meeting=meeting,
            user=user,
            admission_status=MeetingParticipant.ADMISSION_ADMITTED,
            current_breakout=breakout_room,
        ).exists()
    return bool(meeting.whiteboard_student_write_enabled)


def can_clear_whiteboard(user, meeting):
    return is_meeting_teacher(user, meeting)


def can_use_mic(user, meeting, participant=None):
    if not is_meeting_member(user, meeting):
        return False
    if is_meeting_teacher(user, meeting):
        return True
    if not meeting.allow_student_mic:
        return False
    p = participant or MeetingParticipant.objects.filter(meeting=meeting, user=user).first()
    return not (p and p.mic_locked)


def can_use_camera(user, meeting, participant=None):
    if not is_meeting_member(user, meeting):
        return False
    if is_meeting_teacher(user, meeting):
        return True
    if not meeting.allow_student_camera:
        return False
    p = participant or MeetingParticipant.objects.filter(meeting=meeting, user=user).first()
    return not (p and p.camera_locked)


def can_send_chat(user, meeting, participant=None):
    if not is_meeting_member(user, meeting):
        return False
    if is_meeting_teacher(user, meeting):
        return True
    if meeting.chat_mode != Meeting.CHAT_EVERYONE:
        return False
    p = participant or MeetingParticipant.objects.filter(meeting=meeting, user=user).first()
    return not (p and p.chat_muted)


def can_delete_chat_message(user, meeting, message):
    if not is_meeting_member(user, meeting):
        return False
    if is_meeting_teacher(user, meeting):
        return True
    return (
        message.sender_id == user.id
        and message.sender_role not in MeetingParticipant.TEACHER_ROLES
    )


def can_access_recording(user, recording: MeetingRecording):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if is_meeting_teacher(user, recording.meeting) or is_platform_admin(user):
        return True
    return (
        recording.visibility == MeetingRecording.VISIBILITY_PARTICIPANTS
        and is_meeting_member(user, recording.meeting)
    )
