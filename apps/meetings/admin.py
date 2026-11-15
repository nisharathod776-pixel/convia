from django.contrib import admin
from .models import ActivityLog, Meeting, MeetingParticipant


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("meeting_code", "title", "host", "status", "scheduled_at", "created_at", "ended_at")
    list_filter = ("status",)
    search_fields = ("meeting_code", "title", "host__username")


@admin.register(MeetingParticipant)
class MeetingParticipantAdmin(admin.ModelAdmin):
    list_display = ("meeting", "user", "role", "is_active", "joined_at", "left_at")
    list_filter = ("role", "is_active")


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "detail", "created_at")
    list_filter = ("action",)
