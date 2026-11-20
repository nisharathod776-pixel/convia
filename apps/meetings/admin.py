from django.contrib import admin
from .models import (
    ActivityLog,
    AttendanceSession,
    BreakoutRoom,
    Meeting,
    MeetingChatMessage,
    MeetingParticipant,
    MeetingRecording,
)


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = (
        "meeting_code",
        "title",
        "host",
        "status",
        "scheduled_at",
        "admission_deadline",
        "allow_student_mic",
        "allow_student_camera",
        "chat_mode",
        "is_recording",
        "created_at",
        "ended_at",
    )
    list_filter = ("status", "chat_mode", "is_recording")
    search_fields = ("meeting_code", "title", "host__username")


@admin.register(BreakoutRoom)
class BreakoutRoomAdmin(admin.ModelAdmin):
    list_display = ("name", "meeting", "status", "duration_minutes", "opened_at", "ends_at")
    list_filter = ("status",)


@admin.register(MeetingParticipant)
class MeetingParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "meeting",
        "user",
        "role",
        "admission_status",
        "is_active",
        "mic_locked",
        "camera_locked",
        "chat_muted",
        "current_breakout",
        "joined_at",
        "left_at",
    )
    list_filter = ("role", "admission_status", "is_active", "mic_locked", "camera_locked", "chat_muted")


@admin.register(MeetingChatMessage)
class MeetingChatMessageAdmin(admin.ModelAdmin):
    list_display = ("meeting", "breakout_room", "sender", "sender_role", "is_deleted", "created_at")
    list_filter = ("sender_role", "is_deleted")


@admin.register(AttendanceSession)
class AttendanceSessionAdmin(admin.ModelAdmin):
    list_display = ("meeting", "user", "role", "joined_at", "left_at", "duration_seconds")
    list_filter = ("role",)


@admin.register(MeetingRecording)
class MeetingRecordingAdmin(admin.ModelAdmin):
    list_display = ("title", "meeting", "recorded_by", "visibility", "status", "duration_seconds", "file_size", "created_at")
    list_filter = ("visibility", "status")


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "detail", "created_at")
    list_filter = ("action",)
