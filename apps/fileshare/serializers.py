from rest_framework import serializers
from .models import SharedFile


class SharedFileSerializer(serializers.ModelSerializer):
    meeting_code = serializers.CharField(source="meeting.meeting_code", read_only=True)
    uploader_username = serializers.CharField(source="uploaded_by.username", read_only=True, default="Unknown")

    class Meta:
        model = SharedFile
        fields = (
            "id",
            "meeting_code",
            "uploader_username",
            "original_name",
            "file_size",
            "mime_type",
            "uploaded_at",
        )
