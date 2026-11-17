from rest_framework import serializers
from .models import (
    ActivityLog,
    BreakoutRoom,
    Meeting,
    MeetingChatMessage,
    MeetingParticipant,
    MeetingRecording,
)
from .permissions import can_edit_whiteboard, get_meeting_role, is_meeting_teacher


class MeetingParticipantSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    meeting_role = serializers.CharField(source="normalized_role", read_only=True)
    is_host = serializers.BooleanField(source="is_teacher", read_only=True)
    current_breakout_id = serializers.UUIDField(source="current_breakout.id", read_only=True, default=None)

    class Meta:
        model = MeetingParticipant
        fields = (
            "id",
            "user_id",
            "username",
            "full_name",
            "role",
            "meeting_role",
            "is_host",
            "admission_status",
            "is_active",
            "mic_locked",
            "camera_locked",
            "chat_muted",
            "hand_raised",
            "current_breakout_id",
            "requested_at",
            "admitted_at",
            "joined_at",
            "left_at",
        )


class WaitingParticipantSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = MeetingParticipant
        fields = ("id", "user_id", "username", "full_name", "admission_status", "requested_at")

    def get_full_name(self, obj):
        return obj.user.full_name or obj.user.username


class MeetingChatMessageSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    sender_id = serializers.IntegerField(source="sender.id", read_only=True)
    sender_username = serializers.CharField(source="sender.username", read_only=True)
    sender_name = serializers.SerializerMethodField()
    breakout_room_id = serializers.UUIDField(source="breakout_room.id", read_only=True, default=None)
    timestamp = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = MeetingChatMessage
        fields = (
            "id",
            "sender_id",
            "sender_username",
            "sender_name",
            "sender_role",
            "breakout_room_id",
            "text",
            "timestamp",
        )

    def get_sender_name(self, obj):
        return obj.sender.full_name or obj.sender.username


class BreakoutRoomSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    assigned_users = serializers.SerializerMethodField()

    class Meta:
        model = BreakoutRoom
        fields = (
            "id",
            "name",
            "status",
            "duration_minutes",
            "allow_return_to_main",
            "opened_at",
            "ends_at",
            "closed_at",
            "assigned_users",
            "created_at",
        )

    def get_assigned_users(self, obj):
        qs = obj.assigned_participants.filter(
            admission_status=MeetingParticipant.ADMISSION_ADMITTED
        ).select_related("user")
        return [
            {
                "user_id": p.user_id,
                "username": p.user.username,
                "full_name": p.user.full_name or p.user.username,
                "is_active": p.is_active,
            }
            for p in qs
        ]


class MeetingRecordingSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    meeting_code = serializers.CharField(source="meeting.meeting_code", read_only=True)
    recorded_by_username = serializers.CharField(source="recorded_by.username", read_only=True, default=None)

    class Meta:
        model = MeetingRecording
        fields = (
            "id",
            "meeting_code",
            "title",
            "original_name",
            "file_size",
            "mime_type",
            "duration_seconds",
            "visibility",
            "status",
            "recorded_by_username",
            "started_at",
            "ended_at",
            "created_at",
        )


class MeetingSerializer(serializers.ModelSerializer):
    host_username = serializers.CharField(source="host.username", read_only=True)
    is_host = serializers.SerializerMethodField()
    meeting_role = serializers.SerializerMethodField()
    participant_count = serializers.SerializerMethodField()
    active_screen_sharer_id = serializers.IntegerField(source="active_screen_sharer.id", read_only=True, default=None)
    active_screen_sharer_username = serializers.CharField(source="active_screen_sharer.username", read_only=True, default=None)
    can_draw_whiteboard = serializers.SerializerMethodField()
    my_moderation = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = (
            "id",
            "meeting_code",
            "title",
            "host_username",
            "is_host",
            "meeting_role",
            "status",
            "scheduled_at",
            "admission_deadline",
            "waiting_room_enabled",
            "whiteboard_student_write_enabled",
            "allow_student_screenshare",
            "allow_student_mic",
            "allow_student_camera",
            "allow_student_file_share",
            "chat_mode",
            "active_screen_sharer_id",
            "active_screen_sharer_username",
            "active_screen_sharer_sid",
            "is_recording",
            "recording_started_at",
            "can_draw_whiteboard",
            "my_moderation",
            "ended_at",
            "created_at",
            "participant_count",
        )
        read_only_fields = (
            "id",
            "meeting_code",
            "host_username",
            "is_host",
            "meeting_role",
            "status",
            "active_screen_sharer_id",
            "active_screen_sharer_username",
            "active_screen_sharer_sid",
            "is_recording",
            "recording_started_at",
            "can_draw_whiteboard",
            "my_moderation",
            "ended_at",
            "created_at",
        )

    def get_is_host(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return is_meeting_teacher(request.user, obj)

    def get_meeting_role(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return MeetingParticipant.ROLE_STUDENT
        return get_meeting_role(request.user, obj)

    def get_can_draw_whiteboard(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return can_edit_whiteboard(request.user, obj)

    def get_my_moderation(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return {
                "mic_locked": False,
                "camera_locked": False,
                "chat_muted": False,
                "hand_raised": False,
                "current_breakout_id": None,
            }
        p = obj.participants.filter(user=request.user).first()
        if not p:
            return {
                "mic_locked": False,
                "camera_locked": False,
                "chat_muted": False,
                "hand_raised": False,
                "current_breakout_id": None,
            }
        is_teacher = is_meeting_teacher(request.user, obj)
        return {
            "mic_locked": (not is_teacher) and (not obj.allow_student_mic or p.mic_locked),
            "camera_locked": (not is_teacher) and (not obj.allow_student_camera or p.camera_locked),
            "chat_muted": (not is_teacher) and p.chat_muted,
            "hand_raised": p.hand_raised,
            "current_breakout_id": str(p.current_breakout_id) if p.current_breakout_id else None,
        }

    def get_participant_count(self, obj):
        return obj.participants.filter(is_active=True, admission_status=MeetingParticipant.ADMISSION_ADMITTED).count()


class MeetingCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=160, min_length=2)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    admission_deadline = serializers.DateTimeField(required=False, allow_null=True)
    waiting_room_enabled = serializers.BooleanField(required=False, default=True)
    whiteboard_student_write_enabled = serializers.BooleanField(required=False, default=False)
    allow_student_screenshare = serializers.BooleanField(required=False, default=False)
    allow_student_mic = serializers.BooleanField(required=False, default=True)
    allow_student_camera = serializers.BooleanField(required=False, default=True)
    allow_student_file_share = serializers.BooleanField(required=False, default=True)
    chat_mode = serializers.ChoiceField(
        choices=Meeting.CHAT_MODE_CHOICES,
        required=False,
        default=Meeting.CHAT_EVERYONE,
    )

    def validate(self, attrs):
        scheduled_at = attrs.get("scheduled_at")
        admission_deadline = attrs.get("admission_deadline")
        if scheduled_at and admission_deadline and admission_deadline <= scheduled_at:
            raise serializers.ValidationError(
                {"admission_deadline": "Admission deadline must be after the scheduled start time."}
            )
        return attrs


class ActivityLogSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True, default="system")

    class Meta:
        model = ActivityLog
        fields = ("id", "username", "action", "detail", "created_at")
