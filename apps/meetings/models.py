import secrets
import string
import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


def generate_meeting_code():
    alphabet = string.ascii_lowercase + string.digits
    part1 = "".join(secrets.choice(alphabet) for _ in range(4))
    part2 = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"avr-{part1}-{part2}"


def meeting_recording_upload_to(instance, filename):
    ext = ".webm"
    if "." in filename:
        candidate = "." + filename.rsplit(".", 1)[1].lower()
        if candidate in (".webm", ".mp4", ".mkv", ".ogg"):
            ext = candidate
    return f"meeting_recordings/{instance.meeting.meeting_code}/{uuid.uuid4().hex}{ext}"


class Meeting(models.Model):
    STATUS_SCHEDULED = "scheduled"
    STATUS_ACTIVE = "active"
    STATUS_ENDED = "ended"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_ENDED, "Ended"),
    ]

    CHAT_EVERYONE = "everyone"
    CHAT_TEACHER_ONLY = "teacher_only"
    CHAT_DISABLED = "disabled"
    CHAT_MODE_CHOICES = [
        (CHAT_EVERYONE, "Everyone"),
        (CHAT_TEACHER_ONLY, "Teacher Announcements Only"),
        (CHAT_DISABLED, "Disabled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting_code = models.CharField(max_length=24, unique=True, db_index=True)
    title = models.CharField(max_length=160)
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hosted_meetings",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    admission_deadline = models.DateTimeField(null=True, blank=True)
    waiting_room_enabled = models.BooleanField(default=True)
    whiteboard_student_write_enabled = models.BooleanField(default=False)
    allow_student_screenshare = models.BooleanField(default=False)
    allow_student_mic = models.BooleanField(default=True)
    allow_student_camera = models.BooleanField(default=True)
    allow_student_file_share = models.BooleanField(default=True)
    chat_mode = models.CharField(max_length=20, choices=CHAT_MODE_CHOICES, default=CHAT_EVERYONE)
    active_screen_sharer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_screen_shares",
    )
    active_screen_sharer_sid = models.CharField(max_length=128, blank=True, default="")
    is_recording = models.BooleanField(default=False)
    recording_started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="started_recordings",
    )
    recording_started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.meeting_code:
            code = generate_meeting_code()
            while Meeting.objects.filter(meeting_code=code).exists():
                code = generate_meeting_code()
            self.meeting_code = code
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.meeting_code})"


class BreakoutRoom(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="breakout_rooms")
    name = models.CharField(max_length=80)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    allow_return_to_main = models.BooleanField(default=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_breakout_rooms",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "name"]

    def __str__(self):
        return f"{self.name} ({self.meeting.meeting_code}, {self.status})"


class MeetingParticipant(models.Model):
    ROLE_TEACHER = "teacher"
    ROLE_HOST = "host"
    ROLE_STUDENT = "student"
    ROLE_PARTICIPANT = "participant"
    TEACHER_ROLES = (ROLE_TEACHER, ROLE_HOST)
    STUDENT_ROLES = (ROLE_STUDENT, ROLE_PARTICIPANT)
    ROLE_CHOICES = [
        (ROLE_TEACHER, "Teacher / Host"),
        (ROLE_HOST, "Host"),
        (ROLE_STUDENT, "Student / Participant"),
        (ROLE_PARTICIPANT, "Participant"),
    ]

    ADMISSION_WAITING = "waiting"
    ADMISSION_ADMITTED = "admitted"
    ADMISSION_REJECTED = "rejected"
    ADMISSION_REMOVED = "removed"
    ADMISSION_CHOICES = [
        (ADMISSION_WAITING, "Waiting"),
        (ADMISSION_ADMITTED, "Admitted"),
        (ADMISSION_REJECTED, "Rejected"),
        (ADMISSION_REMOVED, "Removed"),
    ]

    ATTENDANCE_OVERRIDES = [
        ("", "Automatic"),
        ("present", "Present"),
        ("late", "Late"),
        ("excused", "Excused"),
        ("absent", "Absent"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="meeting_memberships")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_STUDENT)
    admission_status = models.CharField(
        max_length=16,
        choices=ADMISSION_CHOICES,
        default=ADMISSION_ADMITTED,
        db_index=True,
    )
    is_active = models.BooleanField(default=True)
    mic_locked = models.BooleanField(default=False)
    camera_locked = models.BooleanField(default=False)
    chat_muted = models.BooleanField(default=False)
    hand_raised = models.BooleanField(default=False)
    current_breakout = models.ForeignKey(
        BreakoutRoom,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_participants",
    )
    attendance_status_override = models.CharField(
        max_length=20,
        choices=ATTENDANCE_OVERRIDES,
        blank=True,
        default="",
    )
    attendance_note = models.CharField(max_length=255, blank=True, default="")
    requested_at = models.DateTimeField(null=True, blank=True)
    admitted_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    decision_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admission_decisions",
    )
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("meeting", "user")
        ordering = ["-joined_at"]

    @property
    def is_teacher(self):
        return self.role in self.TEACHER_ROLES or self.meeting.host_id == self.user_id

    @property
    def normalized_role(self):
        return self.ROLE_TEACHER if self.is_teacher else self.ROLE_STUDENT

    def __str__(self):
        return f"{self.user.username} in {self.meeting.meeting_code} ({self.normalized_role}/{self.admission_status})"


class MeetingChatMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="chat_messages")
    breakout_room = models.ForeignKey(
        BreakoutRoom,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="chat_messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meeting_chat_messages",
    )
    sender_role = models.CharField(max_length=20, default=MeetingParticipant.ROLE_STUDENT)
    text = models.TextField(max_length=2000)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deleted_chat_messages",
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Chat {self.id} in {self.meeting.meeting_code} by {self.sender.username}"


class AttendanceSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="attendance_sessions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="attendance_sessions",
    )
    role = models.CharField(max_length=20, default=MeetingParticipant.ROLE_STUDENT)
    joined_at = models.DateTimeField(default=timezone.now, db_index=True)
    left_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["joined_at"]

    def close(self, at_time=None):
        end_ts = at_time or timezone.now()
        if end_ts < self.joined_at:
            end_ts = self.joined_at
        self.left_at = end_ts
        self.duration_seconds = max(0, int((end_ts - self.joined_at).total_seconds()))
        self.save(update_fields=["left_at", "duration_seconds"])

    def __str__(self):
        return f"Attendance {self.user.username} in {self.meeting.meeting_code}"


class MeetingRecording(models.Model):
    VISIBILITY_TEACHER_ONLY = "teacher_only"
    VISIBILITY_PARTICIPANTS = "participants"
    VISIBILITY_CHOICES = [
        (VISIBILITY_TEACHER_ONLY, "Teacher Only"),
        (VISIBILITY_PARTICIPANTS, "All Admitted Participants"),
    ]

    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="recordings")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_sessions",
    )
    title = models.CharField(max_length=160)
    file = models.FileField(upload_to=meeting_recording_upload_to)
    original_name = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, default="video/webm")
    duration_seconds = models.PositiveIntegerField(default=0)
    visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default=VISIBILITY_TEACHER_ONLY)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_READY)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Recording {self.title} ({self.meeting.meeting_code})"


class ActivityLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_logs",
    )
    action = models.CharField(max_length=80)
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} ({self.created_at})"
