from django.contrib import admin
from .models import SharedFile


@admin.register(SharedFile)
class SharedFileAdmin(admin.ModelAdmin):
    list_display = ("original_name", "meeting", "uploaded_by", "file_size", "mime_type", "uploaded_at")
    search_fields = ("original_name", "meeting__meeting_code", "uploaded_by__username")
