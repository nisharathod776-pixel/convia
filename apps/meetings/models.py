import secrets
import string
import uuid
from django.conf import settings
from django.db import models


def generate_meeting_code():
    alphabet = string.ascii_lowercase + string.digits
    part1 = "".join(secrets.choice(alphabet) for _ in range(4))
    part2 = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"avr-{part1}-{part2}"


class Meeting(models.Model):
    STATUS_SCHEDULED = "scheduled"
    STATUS_ACTIVE = "active"
    STATUS_ENDED = "ended"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_ENDED, "Ended"),
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


class MeetingParticipant(models.Model):
    ROLE_HOST = "host"
    ROLE_PARTICIPANT = "participant"
    ROLE_CHOICES = [
        (ROLE_HOST, "Host"),
        (ROLE_PARTICIPANT, "Participant"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="meeting_memberships")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_PARTICIPANT)
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("meeting", "user")
        ordering = ["-joined_at"]

    def __str__(self):
        return f"{self.user.username} in {self.meeting.meeting_code}"


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
