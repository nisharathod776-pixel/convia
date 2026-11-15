from .models import MeetingParticipant


def is_meeting_member(user, meeting):
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.role == "admin":
        return True
    if meeting.host_id == user.id:
        return True
    return MeetingParticipant.objects.filter(meeting=meeting, user=user).exists()
