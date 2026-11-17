from django.conf import settings
from django.db import models

from apps.meetings.models import Meeting


class WhiteboardOperation(models.Model):
    TYPE_DRAW = "draw"
    TYPE_TEXT = "text"
    TYPE_CHOICES = [
        (TYPE_DRAW, "Draw"),
        (TYPE_TEXT, "Text"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="whiteboard_operations")
    breakout_room = models.ForeignKey(
        "meetings.BreakoutRoom",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="whiteboard_operations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    op_id = models.CharField(max_length=64, db_index=True)
    op_type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.meeting.meeting_code} {self.op_type} ({self.op_id})"
