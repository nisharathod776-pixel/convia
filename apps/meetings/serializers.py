from rest_framework import serializers
from .models import ActivityLog, Meeting, MeetingParticipant


class MeetingParticipantSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = MeetingParticipant
        fields = ("id", "username", "full_name", "role", "is_active", "joined_at", "left_at")


class MeetingSerializer(serializers.ModelSerializer):
    host_username = serializers.CharField(source="host.username", read_only=True)
    is_host = serializers.SerializerMethodField()
    participant_count = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = (
            "id",
            "meeting_code",
            "title",
            "host_username",
            "is_host",
            "status",
            "scheduled_at",
            "ended_at",
            "created_at",
            "participant_count",
        )
        read_only_fields = ("id", "meeting_code", "host_username", "is_host", "status", "ended_at", "created_at")

    def get_is_host(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return obj.host_id == request.user.id

    def get_participant_count(self, obj):
        return obj.participants.filter(is_active=True).count()


class MeetingCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=160, min_length=2)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)


class ActivityLogSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True, default="system")

    class Meta:
        model = ActivityLog
        fields = ("id", "username", "action", "detail", "created_at")
