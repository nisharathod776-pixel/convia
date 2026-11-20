import csv
import io
import logging
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.fileshare.models import SharedFile
from apps.fileshare.serializers import SharedFileSerializer
from .models import (
    ActivityLog,
    BreakoutRoom,
    Meeting,
    MeetingChatMessage,
    MeetingParticipant,
    MeetingRecording,
)
from .permissions import (
    can_access_recording,
    can_delete_chat_message,
    is_meeting_member,
    is_meeting_teacher,
    is_platform_admin,
)
from .serializers import (
    BreakoutRoomSerializer,
    MeetingChatMessageSerializer,
    MeetingCreateSerializer,
    MeetingParticipantSerializer,
    MeetingRecordingSerializer,
    MeetingSerializer,
    WaitingParticipantSerializer,
)
from .services import (
    build_attendance_report,
    close_all_attendance_sessions,
    close_user_attendance_sessions,
    csv_safe_cell,
    open_attendance_session,
    validate_recording_upload,
)

logger = logging.getLogger("apps.meetings")


class DashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        meetings = (
            Meeting.objects.filter(
                Q(host=user)
                | Q(
                    participants__user=user,
                    participants__admission_status=MeetingParticipant.ADMISSION_ADMITTED,
                )
            )
            .distinct()
            .select_related("host", "active_screen_sharer")
        )
        shared_files = SharedFile.objects.filter(meeting__in=meetings).distinct()
        recent_files = shared_files.select_related("meeting", "uploaded_by")[:10]

        stats = {
            "total_meetings": meetings.count(),
            "active_meetings": meetings.filter(status=Meeting.STATUS_ACTIVE).count(),
            "hosted_meetings": meetings.filter(host=user).count(),
            "shared_files": shared_files.count(),
        }

        return Response(
            {
                "stats": stats,
                "meetings": MeetingSerializer(meetings[:50], many=True, context={"request": request}).data,
                "recent_files": SharedFileSerializer(recent_files, many=True).data,
            }
        )


class MeetingListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        meetings = (
            Meeting.objects.filter(
                Q(host=user)
                | Q(
                    participants__user=user,
                    participants__admission_status=MeetingParticipant.ADMISSION_ADMITTED,
                )
            )
            .distinct()
            .select_related("host", "active_screen_sharer")
        )
        return Response(MeetingSerializer(meetings, many=True, context={"request": request}).data)

    def post(self, request):
        serializer = MeetingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scheduled_at = serializer.validated_data.get("scheduled_at")
        admission_deadline = serializer.validated_data.get("admission_deadline")
        initial_status = (
            Meeting.STATUS_SCHEDULED
            if scheduled_at and scheduled_at > timezone.now()
            else Meeting.STATUS_ACTIVE
        )
        with transaction.atomic():
            meeting = Meeting.objects.create(
                title=serializer.validated_data["title"].strip(),
                host=request.user,
                status=initial_status,
                scheduled_at=scheduled_at,
                admission_deadline=admission_deadline,
                waiting_room_enabled=serializer.validated_data.get("waiting_room_enabled", True),
                whiteboard_student_write_enabled=serializer.validated_data.get(
                    "whiteboard_student_write_enabled", False
                ),
                allow_student_screenshare=serializer.validated_data.get("allow_student_screenshare", False),
                allow_student_mic=serializer.validated_data.get("allow_student_mic", True),
                allow_student_camera=serializer.validated_data.get("allow_student_camera", True),
                allow_student_file_share=serializer.validated_data.get("allow_student_file_share", True),
                chat_mode=serializer.validated_data.get("chat_mode", Meeting.CHAT_EVERYONE),
            )
            MeetingParticipant.objects.create(
                meeting=meeting,
                user=request.user,
                role=MeetingParticipant.ROLE_TEACHER,
                admission_status=MeetingParticipant.ADMISSION_ADMITTED,
                is_active=True,
                admitted_at=timezone.now(),
            )
            ActivityLog.objects.create(
                user=request.user,
                action="meeting_created",
                detail=f"{meeting.meeting_code}: {meeting.title}",
            )
        logger.info("Meeting %s created by %s", meeting.meeting_code, request.user.username)
        return Response(
            MeetingSerializer(meeting, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class MeetingDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(
            Meeting.objects.select_related("host", "active_screen_sharer"),
            meeting_code=meeting_code.lower(),
        )
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)
        return Response(MeetingSerializer(meeting, context={"request": request}).data)


class MeetingJoinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_code):
        with transaction.atomic():
            meeting = get_object_or_404(
                Meeting.objects.select_for_update(of=("self",)).select_related(
                    "host", "active_screen_sharer"
                ),
                meeting_code=meeting_code.lower(),
            )
            if meeting.status == Meeting.STATUS_ENDED:
                return Response(
                    {"detail": "This meeting has already ended."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            now = timezone.now()
            is_teacher = is_meeting_teacher(request.user, meeting)

            if is_teacher:
                if meeting.status == Meeting.STATUS_SCHEDULED:
                    meeting.status = Meeting.STATUS_ACTIVE
                    meeting.save(update_fields=["status"])

                participant, created = MeetingParticipant.objects.get_or_create(
                    meeting=meeting,
                    user=request.user,
                    defaults={
                        "role": MeetingParticipant.ROLE_TEACHER,
                        "admission_status": MeetingParticipant.ADMISSION_ADMITTED,
                        "is_active": True,
                        "admitted_at": now,
                    },
                )
                if not created:
                    participant.is_active = True
                    participant.left_at = None
                    participant.admission_status = MeetingParticipant.ADMISSION_ADMITTED
                    if participant.role not in MeetingParticipant.TEACHER_ROLES:
                        participant.role = MeetingParticipant.ROLE_TEACHER
                    if not participant.admitted_at:
                        participant.admitted_at = now
                    participant.save(
                        update_fields=["is_active", "left_at", "admission_status", "role", "admitted_at"]
                    )

                open_attendance_session(meeting, request.user, MeetingParticipant.ROLE_TEACHER, at_time=now)
                ActivityLog.objects.create(
                    user=request.user,
                    action="meeting_joined",
                    detail=f"{meeting.meeting_code} (teacher)",
                )
                data = MeetingSerializer(meeting, context={"request": request}).data
                data["admission_status"] = MeetingParticipant.ADMISSION_ADMITTED
                data["participants"] = MeetingParticipantSerializer(
                    meeting.participants.filter(
                        is_active=True, admission_status=MeetingParticipant.ADMISSION_ADMITTED
                    ).select_related("user", "current_breakout"),
                    many=True,
                ).data
                data["waiting_room"] = WaitingParticipantSerializer(
                    meeting.participants.filter(
                        admission_status=MeetingParticipant.ADMISSION_WAITING
                    ).select_related("user"),
                    many=True,
                ).data
                return Response(data)

            # Student / non-teacher flow
            participant = (
                MeetingParticipant.objects.select_for_update()
                .filter(meeting=meeting, user=request.user)
                .first()
            )

            if participant and participant.admission_status == MeetingParticipant.ADMISSION_REMOVED:
                return Response(
                    {
                        "detail": "You were removed from this meeting by the teacher.",
                        "code": "admission_removed",
                        "admission_status": MeetingParticipant.ADMISSION_REMOVED,
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            if participant and participant.admission_status == MeetingParticipant.ADMISSION_ADMITTED:
                if meeting.status == Meeting.STATUS_SCHEDULED:
                    meeting.status = Meeting.STATUS_ACTIVE
                    meeting.save(update_fields=["status"])
                participant.is_active = True
                participant.left_at = None
                participant.save(update_fields=["is_active", "left_at"])
                open_attendance_session(meeting, request.user, MeetingParticipant.ROLE_STUDENT, at_time=now)
                ActivityLog.objects.create(
                    user=request.user,
                    action="meeting_joined",
                    detail=f"{meeting.meeting_code} (admitted student)",
                )
                data = MeetingSerializer(meeting, context={"request": request}).data
                data["admission_status"] = MeetingParticipant.ADMISSION_ADMITTED
                data["participants"] = MeetingParticipantSerializer(
                    meeting.participants.filter(
                        is_active=True, admission_status=MeetingParticipant.ADMISSION_ADMITTED
                    ).select_related("user", "current_breakout"),
                    many=True,
                ).data
                return Response(data)

            # Enforce server-side admission window
            if meeting.scheduled_at and now < meeting.scheduled_at:
                return Response(
                    {
                        "detail": "This meeting has not started yet. Admission opens at the scheduled start time.",
                        "code": "not_started",
                        "scheduled_at": meeting.scheduled_at.isoformat(),
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            if meeting.admission_deadline and now > meeting.admission_deadline:
                return Response(
                    {
                        "detail": "The meeting admission time has expired. Please contact your teacher.",
                        "code": "admission_expired",
                        "admission_deadline": meeting.admission_deadline.isoformat(),
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Guests are admitted immediately; host approval is disabled.
            if meeting.status == Meeting.STATUS_SCHEDULED:
                meeting.status = Meeting.STATUS_ACTIVE
                meeting.save(update_fields=["status"])
            if participant is None:
                participant = MeetingParticipant.objects.create(
                    meeting=meeting,
                    user=request.user,
                    role=MeetingParticipant.ROLE_STUDENT,
                    admission_status=MeetingParticipant.ADMISSION_ADMITTED,
                    is_active=True,
                    admitted_at=now,
                )
            else:
                participant.admission_status = MeetingParticipant.ADMISSION_ADMITTED
                participant.is_active = True
                participant.left_at = None
                participant.admitted_at = participant.admitted_at or now
                participant.save(update_fields=["admission_status", "is_active", "left_at", "admitted_at"])

            open_attendance_session(meeting, request.user, MeetingParticipant.ROLE_STUDENT, at_time=now)
            ActivityLog.objects.create(
                user=request.user,
                action="meeting_joined",
                detail=meeting.meeting_code,
            )
            data = MeetingSerializer(meeting, context={"request": request}).data
            data["admission_status"] = MeetingParticipant.ADMISSION_ADMITTED
            data["participants"] = MeetingParticipantSerializer(
                meeting.participants.filter(
                    is_active=True, admission_status=MeetingParticipant.ADMISSION_ADMITTED
                ).select_related("user", "current_breakout"),
                many=True,
            ).data
            return Response(data)



class MeetingAdmissionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(
            Meeting,
            meeting_code=meeting_code.lower()
        )

        # Only the original Host can view admission requests.
        if meeting.host_id != request.user.id:
            return Response(
                {"detail": "Only the Host can view admission requests."},
                status=status.HTTP_403_FORBIDDEN,
            )

        waiting = (
            meeting.participants.filter(
                admission_status=MeetingParticipant.ADMISSION_WAITING
            )
            .select_related("user")
            .order_by("requested_at", "joined_at")
        )

        return Response({
            "waiting_room": WaitingParticipantSerializer(
                waiting,
                many=True
            ).data
        })

    def post(self, request, meeting_code):
        decision = str(
            request.data.get("decision", "")
        ).strip().lower()

        target_user_id = request.data.get("user_id")

        if decision not in ("admit", "reject"):
            return Response(
                {"detail": "Field 'decision' must be 'admit' or 'reject'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            target_user_id = int(target_user_id)
        except (TypeError, ValueError):
            return Response(
                {"detail": "Valid integer 'user_id' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            meeting = get_object_or_404(
                Meeting.objects.select_for_update(),
                meeting_code=meeting_code.lower()
            )

            # Only the original Host can manage admission requests.
            if meeting.host_id != request.user.id:
                ActivityLog.objects.create(
                    user=request.user,
                    action="unauthorized_admission_decision",
                    detail=(
                        f"{meeting.meeting_code}: "
                        f"user_id={target_user_id} decision={decision}"
                    ),
                )

                return Response(
                    {"detail": "Only the Host can manage admission requests."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if meeting.status == Meeting.STATUS_ENDED:
                return Response(
                    {"detail": "Meeting has already ended."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            participant = (
                MeetingParticipant.objects.select_for_update()
                .select_related("user")
                .filter(
                    meeting=meeting,
                    user_id=target_user_id
                )
                .first()
            )

            if not participant:
                return Response(
                    {"detail": "Admission request not found for this Participant."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if participant.is_teacher:
                return Response(
                    {"detail": "Cannot modify admission status for the Host."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if participant.admission_status != MeetingParticipant.ADMISSION_WAITING:
                return Response(
                    {
                        "detail": (
                            f"Request is already "
                            f"{participant.admission_status}."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            now = timezone.now()

            if decision == "admit":
                if meeting.status == Meeting.STATUS_SCHEDULED:
                    meeting.status = Meeting.STATUS_ACTIVE
                    meeting.save(update_fields=["status"])

                participant.admission_status = (
                    MeetingParticipant.ADMISSION_ADMITTED
                )
                participant.is_active = True
                participant.admitted_at = now
                participant.left_at = None
                participant.decision_by = request.user

                participant.save(
                    update_fields=[
                        "admission_status",
                        "is_active",
                        "admitted_at",
                        "left_at",
                        "decision_by",
                    ]
                )

                ActivityLog.objects.create(
                    user=request.user,
                    action="admission_admitted",
                    detail=f"{meeting.meeting_code}: @{participant.user.username}",
                )

            else:
                participant.admission_status = (
                    MeetingParticipant.ADMISSION_REJECTED
                )
                participant.is_active = False
                participant.rejected_at = now
                participant.decision_by = request.user

                participant.save(
                    update_fields=[
                        "admission_status",
                        "is_active",
                        "rejected_at",
                        "decision_by",
                    ]
                )

                ActivityLog.objects.create(
                    user=request.user,
                    action="admission_rejected",
                    detail=f"{meeting.meeting_code}: @{participant.user.username}",
                )

            waiting_qs = (
                meeting.participants.filter(
                    admission_status=MeetingParticipant.ADMISSION_WAITING
                )
                .select_related("user")
                .order_by("requested_at", "joined_at")
            )

            return Response({
                "participant": WaitingParticipantSerializer(
                    participant
                ).data,
                "admission_status": participant.admission_status,
                "waiting_room": WaitingParticipantSerializer(
                    waiting_qs,
                    many=True
                ).data,
            })

class MeetingPermissionsView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, meeting_code):
        with transaction.atomic():
            meeting = get_object_or_404(
                Meeting.objects.select_for_update().select_related("host", "active_screen_sharer"),
                meeting_code=meeting_code.lower(),
            )
            if not is_meeting_teacher(request.user, meeting):
                ActivityLog.objects.create(
                    user=request.user,
                    action="unauthorized_permission_change",
                    detail=meeting.meeting_code,
                )
                return Response(
                    {"detail": "Only the meeting teacher can update meeting permissions."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            updated_fields = []
            changes = []

            for bool_field, log_name in (
                ("whiteboard_student_write_enabled", "wb_student"),
                ("allow_student_screenshare", "student_screenshare"),
                ("waiting_room_enabled", "waiting_room"),
                ("allow_student_mic", "student_mic"),
                ("allow_student_camera", "student_camera"),
                ("allow_student_file_share", "student_file_share"),
            ):
                if bool_field in request.data:
                    val = request.data.get(bool_field)
                    if not isinstance(val, bool):
                        return Response(
                            {"detail": f"{bool_field} must be a boolean."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    setattr(meeting, bool_field, val)
                    updated_fields.append(bool_field)
                    changes.append(f"{log_name}={val}")

                    if (
                        bool_field == "allow_student_screenshare"
                        and not val
                        and meeting.active_screen_sharer_id
                        and meeting.active_screen_sharer_id != meeting.host_id
                    ):
                        meeting.active_screen_sharer = None
                        meeting.active_screen_sharer_sid = ""
                        updated_fields.extend(["active_screen_sharer", "active_screen_sharer_sid"])

            if "chat_mode" in request.data:
                mode = str(request.data.get("chat_mode", "")).strip()
                valid_modes = {choice[0] for choice in Meeting.CHAT_MODE_CHOICES}
                if mode not in valid_modes:
                    return Response(
                        {"detail": f"chat_mode must be one of: {', '.join(sorted(valid_modes))}."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                meeting.chat_mode = mode
                updated_fields.append("chat_mode")
                changes.append(f"chat_mode={mode}")

            if "admission_deadline" in request.data:
                raw_deadline = request.data.get("admission_deadline")
                if raw_deadline in (None, ""):
                    meeting.admission_deadline = None
                else:
                    parsed = parse_datetime(str(raw_deadline))
                    if parsed is None:
                        return Response(
                            {"detail": "Invalid ISO datetime for admission_deadline."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    if timezone.is_naive(parsed):
                        parsed = timezone.make_aware(parsed, timezone.utc)
                    if meeting.scheduled_at and parsed <= meeting.scheduled_at:
                        return Response(
                            {"detail": "Admission deadline must be after the scheduled start time."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    meeting.admission_deadline = parsed
                updated_fields.append("admission_deadline")
                changes.append(f"deadline={meeting.admission_deadline}")

            if updated_fields:
                meeting.save(update_fields=list(dict.fromkeys(updated_fields)))
                ActivityLog.objects.create(
                    user=request.user,
                    action="meeting_permissions_updated",
                    detail=f"{meeting.meeting_code}: {', '.join(changes)}",
                )

            return Response(MeetingSerializer(meeting, context={"request": request}).data)


class MeetingModerationView(APIView):
    permission_classes = [IsAuthenticated]

    BULK_ACTIONS = {"mute_all_mics", "unmute_all_mics", "disable_all_cams", "enable_all_cams"}
    USER_ACTIONS = {
        "mute_mic",
        "unmute_mic",
        "lock_mic",
        "unlock_mic",
        "disable_cam",
        "enable_cam",
        "mute_chat",
        "unmute_chat",
        "lower_hand",
        "remove",
    }

    def post(self, request, meeting_code):
        action = str(request.data.get("action", "")).strip().lower()
        if action not in self.BULK_ACTIONS and action not in self.USER_ACTIONS:
            return Response({"detail": "Invalid moderation action."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            meeting = get_object_or_404(Meeting.objects.select_for_update(), meeting_code=meeting_code.lower())
            if not is_meeting_teacher(request.user, meeting):
                ActivityLog.objects.create(
                    user=request.user,
                    action="unauthorized_moderation",
                    detail=f"{meeting.meeting_code}: action={action}",
                )
                return Response(
                    {"detail": "Only the meeting teacher can moderate participants."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            students_qs = meeting.participants.select_for_update().filter(
                admission_status=MeetingParticipant.ADMISSION_ADMITTED,
            ).exclude(user_id=meeting.host_id).exclude(role__in=MeetingParticipant.TEACHER_ROLES)

            if action in self.BULK_ACTIONS:
                if action == "mute_all_mics":
                    meeting.allow_student_mic = False
                    meeting.save(update_fields=["allow_student_mic"])
                    students_qs.update(mic_locked=True)
                elif action == "unmute_all_mics":
                    meeting.allow_student_mic = True
                    meeting.save(update_fields=["allow_student_mic"])
                    students_qs.update(mic_locked=False)
                elif action == "disable_all_cams":
                    meeting.allow_student_camera = False
                    meeting.save(update_fields=["allow_student_camera"])
                    students_qs.update(camera_locked=True)
                elif action == "enable_all_cams":
                    meeting.allow_student_camera = True
                    meeting.save(update_fields=["allow_student_camera"])
                    students_qs.update(camera_locked=False)

                ActivityLog.objects.create(
                    user=request.user,
                    action="bulk_moderation",
                    detail=f"{meeting.meeting_code}: {action}",
                )
                return Response(
                    {
                        "action": action,
                        "meeting": MeetingSerializer(meeting, context={"request": request}).data,
                    }
                )

            try:
                target_user_id = int(request.data.get("user_id"))
            except (TypeError, ValueError):
                return Response({"detail": "Valid integer 'user_id' is required."}, status=status.HTTP_400_BAD_REQUEST)

            participant = (
                MeetingParticipant.objects.select_for_update()
                .select_related("user")
                .filter(meeting=meeting, user_id=target_user_id)
                .first()
            )
            if not participant:
                return Response({"detail": "Participant not found in meeting."}, status=status.HTTP_404_NOT_FOUND)
            if participant.is_teacher:
                return Response(
                    {"detail": "Cannot moderate the meeting teacher."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            now = timezone.now()
            if action in ("mute_mic", "lock_mic"):
                participant.mic_locked = True
                participant.save(update_fields=["mic_locked"])
            elif action in ("unmute_mic", "unlock_mic"):
                participant.mic_locked = False
                participant.save(update_fields=["mic_locked"])
            elif action == "disable_cam":
                participant.camera_locked = True
                participant.save(update_fields=["camera_locked"])
            elif action == "enable_cam":
                participant.camera_locked = False
                participant.save(update_fields=["camera_locked"])
            elif action == "mute_chat":
                participant.chat_muted = True
                participant.save(update_fields=["chat_muted"])
            elif action == "unmute_chat":
                participant.chat_muted = False
                participant.save(update_fields=["chat_muted"])
            elif action == "lower_hand":
                participant.hand_raised = False
                participant.save(update_fields=["hand_raised"])
            elif action == "remove":
                participant.admission_status = MeetingParticipant.ADMISSION_REMOVED
                participant.is_active = False
                participant.removed_at = now
                participant.left_at = now
                participant.hand_raised = False
                participant.current_breakout = None
                participant.save(
                    update_fields=[
                        "admission_status",
                        "is_active",
                        "removed_at",
                        "left_at",
                        "hand_raised",
                        "current_breakout",
                    ]
                )
                close_user_attendance_sessions(meeting, participant.user_id, at_time=now)
                if meeting.active_screen_sharer_id == participant.user_id:
                    meeting.active_screen_sharer = None
                    meeting.active_screen_sharer_sid = ""
                    meeting.save(update_fields=["active_screen_sharer", "active_screen_sharer_sid"])

            ActivityLog.objects.create(
                user=request.user,
                action="participant_moderated",
                detail=f"{meeting.meeting_code}: {action} @{participant.user.username}",
            )
            return Response(
                {
                    "action": action,
                    "participant": MeetingParticipantSerializer(participant).data,
                }
            )


class MeetingChatMessagesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)

        breakout_id = request.query_params.get("breakout_room_id")
        qs = meeting.chat_messages.filter(is_deleted=False).select_related("sender", "breakout_room")
        if breakout_id:
            if not is_meeting_teacher(request.user, meeting):
                is_assigned = MeetingParticipant.objects.filter(
                    meeting=meeting,
                    user=request.user,
                    current_breakout_id=breakout_id,
                ).exists()
                if not is_assigned:
                    return Response(
                        {"detail": "Not authorized for this breakout room chat."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            qs = qs.filter(breakout_room_id=breakout_id)
        else:
            qs = qs.filter(breakout_room__isnull=True)

        return Response({"messages": MeetingChatMessageSerializer(qs[:200], many=True).data})

    def delete(self, request, meeting_code, message_id):
        with transaction.atomic():
            meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
            if not is_meeting_member(request.user, meeting):
                return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)
            msg = get_object_or_404(
                MeetingChatMessage.objects.select_for_update().select_related("sender"),
                id=message_id,
                meeting=meeting,
            )
            if msg.is_deleted:
                return Response({"detail": "Message already deleted."})

            if not can_delete_chat_message(request.user, meeting, msg):
                ActivityLog.objects.create(
                    user=request.user,
                    action="unauthorized_chat_delete",
                    detail=f"{meeting.meeting_code}: msg={msg.id}",
                )
                return Response(
                    {"detail": "You do not have permission to delete this message."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            msg.is_deleted = True
            msg.deleted_by = request.user
            msg.deleted_at = timezone.now()
            msg.save(update_fields=["is_deleted", "deleted_by", "deleted_at"])
            ActivityLog.objects.create(
                user=request.user,
                action="chat_message_deleted",
                detail=f"{meeting.meeting_code}: msg={msg.id} sender=@{msg.sender.username}",
            )
            return Response({"detail": "Message deleted.", "message_id": str(msg.id)})


class MeetingBreakoutRoomsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)
        rooms = meeting.breakout_rooms.prefetch_related("assigned_participants__user")
        me = meeting.participants.filter(user=request.user).select_related("current_breakout").first()
        return Response(
            {
                "rooms": BreakoutRoomSerializer(rooms, many=True).data,
                "my_breakout_id": str(me.current_breakout_id) if me and me.current_breakout_id else None,
            }
        )

    def post(self, request, meeting_code):
        with transaction.atomic():
            meeting = get_object_or_404(Meeting.objects.select_for_update(), meeting_code=meeting_code.lower())
            if not is_meeting_teacher(request.user, meeting):
                return Response(
                    {"detail": "Only the meeting teacher can create breakout rooms."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if meeting.status == Meeting.STATUS_ENDED:
                return Response({"detail": "Meeting has ended."}, status=status.HTTP_400_BAD_REQUEST)

            name = str(request.data.get("name", "")).strip()
            count = request.data.get("count")
            duration_minutes = request.data.get("duration_minutes")
            allow_return = request.data.get("allow_return_to_main", True)
            auto_assign = bool(request.data.get("auto_assign", False))

            if duration_minutes is not None:
                try:
                    duration_minutes = int(duration_minutes)
                    if duration_minutes < 1 or duration_minutes > 240:
                        raise ValueError()
                except (TypeError, ValueError):
                    return Response(
                        {"detail": "duration_minutes must be between 1 and 240."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            created_rooms = []
            names_list = request.data.get("names")
            if isinstance(names_list, list) and names_list:
                for raw_name in names_list[:20]:
                    clean_name = str(raw_name or "").strip()[:80]
                    if clean_name:
                        created_rooms.append(
                            BreakoutRoom.objects.create(
                                meeting=meeting,
                                name=clean_name,
                                duration_minutes=duration_minutes,
                                allow_return_to_main=bool(allow_return),
                                created_by=request.user,
                            )
                        )
            elif name:
                created_rooms.append(
                    BreakoutRoom.objects.create(
                        meeting=meeting,
                        name=name[:80],
                        duration_minutes=duration_minutes,
                        allow_return_to_main=bool(allow_return),
                        created_by=request.user,
                    )
                )
            else:
                try:
                    count_int = int(count or 2)
                    if count_int < 1 or count_int > 20:
                        raise ValueError()
                except (TypeError, ValueError):
                    return Response(
                        {"detail": "Room count must be between 1 and 20."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                existing_count = meeting.breakout_rooms.exclude(status=BreakoutRoom.STATUS_CLOSED).count()
                for idx in range(existing_count + 1, existing_count + count_int + 1):
                    created_rooms.append(
                        BreakoutRoom.objects.create(
                            meeting=meeting,
                            name=f"Breakout Room {idx}",
                            duration_minutes=duration_minutes,
                            allow_return_to_main=bool(allow_return),
                            created_by=request.user,
                        )
                    )

            if auto_assign and created_rooms:
                students = list(
                    meeting.participants.select_for_update()
                    .filter(admission_status=MeetingParticipant.ADMISSION_ADMITTED, is_active=True)
                    .exclude(user_id=meeting.host_id)
                    .exclude(role__in=MeetingParticipant.TEACHER_ROLES)
                    .order_by("joined_at")
                )
                for idx, student in enumerate(students):
                    student.current_breakout = created_rooms[idx % len(created_rooms)]
                    student.save(update_fields=["current_breakout"])

            ActivityLog.objects.create(
                user=request.user,
                action="breakout_rooms_created",
                detail=f"{meeting.meeting_code}: count={len(created_rooms)}",
            )
            rooms = meeting.breakout_rooms.prefetch_related("assigned_participants__user")
            return Response(
                {"rooms": BreakoutRoomSerializer(rooms, many=True).data},
                status=status.HTTP_201_CREATED,
            )


class MeetingBreakoutDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, meeting_code, breakout_id):
        with transaction.atomic():
            meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
            if not is_meeting_teacher(request.user, meeting):
                return Response(
                    {"detail": "Only the meeting teacher can modify breakout rooms."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            room = get_object_or_404(BreakoutRoom.objects.select_for_update(), id=breakout_id, meeting=meeting)
            if "name" in request.data:
                new_name = str(request.data.get("name", "")).strip()
                if len(new_name) < 2:
                    return Response({"detail": "Room name must be at least 2 characters."}, status=status.HTTP_400_BAD_REQUEST)
                room.name = new_name[:80]
            if "allow_return_to_main" in request.data and isinstance(request.data["allow_return_to_main"], bool):
                room.allow_return_to_main = request.data["allow_return_to_main"]
            room.save()
            return Response(BreakoutRoomSerializer(room).data)

    def delete(self, request, meeting_code, breakout_id):
        with transaction.atomic():
            meeting = get_object_or_404(
                Meeting.objects.select_for_update(),
                meeting_code=meeting_code.lower(),
            )

            if not is_meeting_teacher(request.user, meeting):
                return Response(
                    {"detail": "Only the meeting teacher can delete breakout rooms."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            room = get_object_or_404(
                BreakoutRoom.objects.select_for_update(),
                id=breakout_id,
                meeting=meeting,
            )

            if room.status == BreakoutRoom.STATUS_OPEN:
                return Response(
                    {"detail": "Close the breakout room before deleting it."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if room.assigned_participants.exists():
                return Response(
                    {"detail": "Unassign all participants before deleting this room."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            room.delete()
            return Response({"detail": "Breakout room deleted."})



class MeetingBreakoutActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_code):
        action = str(request.data.get("action", "")).strip().lower()
        with transaction.atomic():
            meeting = get_object_or_404(Meeting.objects.select_for_update(), meeting_code=meeting_code.lower())
            if not is_meeting_member(request.user, meeting):
                return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)
            if action == "return_to_main":
                p = meeting.participants.select_for_update().select_related("current_breakout").filter(user=request.user).first()
                if p and p.current_breakout:
                    if not is_meeting_teacher(request.user, meeting) and not p.current_breakout.allow_return_to_main:
                        return Response(
                            {"detail": "Teacher has locked students in breakout rooms until closed."},
                            status=status.HTTP_403_FORBIDDEN,
                        )
                    p.current_breakout = None
                    p.save(update_fields=["current_breakout"])
                return Response({"detail": "Returned to main room.", "my_breakout_id": None})

            if not is_meeting_teacher(request.user, meeting):
                return Response(
                    {"detail": "Only the meeting teacher can manage breakout rooms."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            now = timezone.now()
            if action == "assign":
                raw_ids = request.data.get("user_ids")
                target_ids = []
                if isinstance(raw_ids, list) and raw_ids:
                    try:
                        target_ids = [int(uid) for uid in raw_ids]
                    except (TypeError, ValueError):
                        return Response({"detail": "Valid integer user_ids are required."}, status=status.HTTP_400_BAD_REQUEST)
                else:
                    try:
                        target_ids = [int(request.data.get("user_id"))]
                    except (TypeError, ValueError):
                        return Response({"detail": "Valid integer user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

                room_id = request.data.get("breakout_room_id")
                if room_id in (None, ""):
                    room_id = request.data.get("breakout_id")
                target_room = None if room_id in (None, "") else get_object_or_404(BreakoutRoom, id=room_id, meeting=meeting)

                for target_user_id in target_ids:
                    participant = (
                        meeting.participants.select_for_update()
                        .filter(user_id=target_user_id, admission_status=MeetingParticipant.ADMISSION_ADMITTED)
                        .first()
                    )
                    if not participant:
                        return Response({"detail": "Admitted participant not found."}, status=status.HTTP_404_NOT_FOUND)
                    participant.current_breakout = target_room
                    participant.save(update_fields=["current_breakout"])

            elif action == "set_return_policy":
                allow_ret = bool(request.data.get("allow_return_to_main", True))
                meeting.breakout_rooms.exclude(status=BreakoutRoom.STATUS_CLOSED).update(
                    allow_return_to_main=allow_ret
                )

            elif action == "auto_assign":
                open_or_draft = list(meeting.breakout_rooms.exclude(status=BreakoutRoom.STATUS_CLOSED))
                if not open_or_draft:
                    return Response({"detail": "Create breakout rooms first."}, status=status.HTTP_400_BAD_REQUEST)
                students = list(
                    meeting.participants.select_for_update()
                    .filter(admission_status=MeetingParticipant.ADMISSION_ADMITTED, is_active=True)
                    .exclude(user_id=meeting.host_id)
                    .exclude(role__in=MeetingParticipant.TEACHER_ROLES)
                    .order_by("joined_at")
                )
                for idx, student in enumerate(students):
                    student.current_breakout = open_or_draft[idx % len(open_or_draft)]
                    student.save(update_fields=["current_breakout"])

            elif action == "open_all":
                rooms = list(meeting.breakout_rooms.select_for_update().exclude(status=BreakoutRoom.STATUS_CLOSED))
                if not rooms:
                    return Response({"detail": "No breakout rooms available to open."}, status=status.HTTP_400_BAD_REQUEST)
                dur = request.data.get("duration_minutes")
                ends_at = None
                if dur is not None and str(dur).strip() != "":
                    try:
                        dur_int = int(dur)
                        if dur_int < 1 or dur_int > 240:
                            raise ValueError()
                        ends_at = now + timedelta(minutes=dur_int)
                    except (TypeError, ValueError):
                        return Response(
                            {"detail": "duration_minutes must be between 1 and 240."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                else:
                    dur_int = None
                for room in rooms:
                    room.status = BreakoutRoom.STATUS_OPEN
                    room.opened_at = now
                    room.closed_at = None
                    if dur_int is not None:
                        room.duration_minutes = dur_int
                        room.ends_at = ends_at
                    elif room.duration_minutes:
                        room.ends_at = now + timedelta(minutes=room.duration_minutes)
                    room.save(update_fields=["status", "opened_at", "closed_at", "duration_minutes", "ends_at"])
                ActivityLog.objects.create(
                    user=request.user,
                    action="breakouts_opened",
                    detail=f"{meeting.meeting_code}: {len(rooms)} rooms",
                )

            elif action == "close_all":
                meeting.breakout_rooms.filter(status=BreakoutRoom.STATUS_OPEN).update(
                    status=BreakoutRoom.STATUS_CLOSED,
                    closed_at=now,
                )
                meeting.participants.update(current_breakout=None)
                ActivityLog.objects.create(
                    user=request.user,
                    action="breakouts_closed",
                    detail=meeting.meeting_code,
                )

            else:
                return Response({"detail": "Unsupported breakout action."}, status=status.HTTP_400_BAD_REQUEST)

            rooms = meeting.breakout_rooms.prefetch_related("assigned_participants__user")
            return Response({"rooms": BreakoutRoomSerializer(rooms, many=True).data})


class MeetingAttendanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not (is_meeting_teacher(request.user, meeting) or is_meeting_member(request.user, meeting) or is_platform_admin(request.user)):
            return Response({"detail": "Not authorized to view attendance."}, status=status.HTTP_403_FORBIDDEN)
        return Response(build_attendance_report(meeting, request.user))

    def patch(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_teacher(request.user, meeting):
            return Response(
                {"detail": "Only the meeting teacher can update attendance records."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            target_user_id = int(request.data.get("user_id"))
        except (TypeError, ValueError):
            return Response({"detail": "Valid integer user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        participant = get_object_or_404(MeetingParticipant, meeting=meeting, user_id=target_user_id)
        updated_fields = []
        if "status_override" in request.data:
            val = str(request.data.get("status_override") or "").strip().lower()
            valid = {choice[0] for choice in MeetingParticipant.ATTENDANCE_OVERRIDES}
            if val not in valid:
                return Response(
                    {"detail": f"Invalid status_override. Allowed: {', '.join(sorted(v or 'auto' for v in valid))}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            participant.attendance_status_override = val
            updated_fields.append("attendance_status_override")
        if "note" in request.data:
            participant.attendance_note = str(request.data.get("note") or "")[:255]
            updated_fields.append("attendance_note")
        if updated_fields:
            participant.save(update_fields=updated_fields)
            ActivityLog.objects.create(
                user=request.user,
                action="attendance_updated",
                detail=f"{meeting.meeting_code}: @{participant.user.username}",
            )
        return Response(build_attendance_report(meeting, request.user))


class MeetingAttendanceExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not (is_meeting_teacher(request.user, meeting) or is_platform_admin(request.user)):
            return Response(
                {"detail": "Only the meeting teacher or administrator can export attendance."},
                status=status.HTTP_403_FORBIDDEN,
            )
        report = build_attendance_report(meeting, request.user)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Meeting Code",
                "Name",
                "Username",
                "Email",
                "Role",
                "Admission Status",
                "Attendance Status",
                "Connected Now",
                "First Joined (UTC)",
                "Last Left (UTC)",
                "Join Count",
                "Duration",
                "Duration (Seconds)",
                "Note",
            ]
        )
        for rec in report["records"]:
            writer.writerow(
                [
                    csv_safe_cell(meeting.meeting_code),
                    csv_safe_cell(rec["full_name"]),
                    csv_safe_cell(rec["username"]),
                    csv_safe_cell(rec["email"]),
                    csv_safe_cell(rec["meeting_role"]),
                    csv_safe_cell(rec["admission_status"]),
                    csv_safe_cell(rec["status"]),
                    "Yes" if rec["is_connected"] else "No",
                    csv_safe_cell(rec["first_joined_at"] or ""),
                    csv_safe_cell(rec["last_left_at"] or ""),
                    rec["join_count"],
                    csv_safe_cell(rec["duration_formatted"]),
                    rec["total_duration_seconds"],
                    csv_safe_cell(rec["note"]),
                ]
            )

        resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="attendance-{meeting.meeting_code}.csv"'
        return resp


class MeetingRecordingsView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not (is_meeting_teacher(request.user, meeting) or is_meeting_member(request.user, meeting) or is_platform_admin(request.user)):
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)

        qs = meeting.recordings.select_related("meeting", "recorded_by")
        if not (is_meeting_teacher(request.user, meeting) or is_platform_admin(request.user)):
            qs = qs.filter(visibility=MeetingRecording.VISIBILITY_PARTICIPANTS)
        return Response(
            {
                "is_recording": meeting.is_recording,
                "recording_started_at": meeting.recording_started_at.isoformat() if meeting.recording_started_at else None,
                "recordings": MeetingRecordingSerializer(qs, many=True).data,
            }
        )

    def post(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_teacher(request.user, meeting):
            return Response(
                {"detail": "Only the meeting teacher can save session recordings."},
                status=status.HTTP_403_FORBIDDEN,
            )

        uploaded = request.FILES.get("file")
        original_name, mime_type = validate_recording_upload(uploaded)
        upload_id = str(request.data.get("upload_id") or "").strip()
        if upload_id:
            try:
                upload_id = str(uuid.UUID(upload_id))
            except (TypeError, ValueError, AttributeError):
                return Response({"detail": "Invalid recording upload identifier."}, status=status.HTTP_400_BAD_REQUEST)
            if upload_id not in original_name:
                return Response({"detail": "Recording filename does not match its upload identifier."}, status=status.HTTP_400_BAD_REQUEST)

        title = str(request.data.get("title") or f"{meeting.title} Recording").strip()[:160]
        visibility = str(request.data.get("visibility") or MeetingRecording.VISIBILITY_TEACHER_ONLY).strip()
        if visibility not in {c[0] for c in MeetingRecording.VISIBILITY_CHOICES}:
            visibility = MeetingRecording.VISIBILITY_TEACHER_ONLY

        try:
            duration_seconds = max(0, int(request.data.get("duration_seconds") or 0))
        except (TypeError, ValueError):
            duration_seconds = 0

        now = timezone.now()
        started_at = meeting.recording_started_at or (now - timedelta(seconds=duration_seconds))

        with transaction.atomic():
            meeting = Meeting.objects.select_for_update().get(pk=meeting.pk)
            if upload_id:
                existing = MeetingRecording.objects.filter(
                    meeting=meeting,
                    recorded_by=request.user,
                    original_name=original_name,
                ).first()
                if existing:
                    same_upload = (
                        existing.file_size == uploaded.size
                        and existing.title == title
                        and existing.duration_seconds == duration_seconds
                        and existing.visibility == visibility
                    )
                    if not same_upload:
                        return Response(
                            {"detail": "This upload identifier was already used for a different recording."},
                            status=status.HTTP_409_CONFLICT,
                        )
                    return Response(MeetingRecordingSerializer(existing).data, status=status.HTTP_200_OK)

            rec = MeetingRecording.objects.create(
                meeting=meeting,
                recorded_by=request.user,
                title=title,
                file=uploaded,
                original_name=original_name,
                file_size=uploaded.size,
                mime_type=mime_type,
                duration_seconds=duration_seconds,
                visibility=visibility,
                status=MeetingRecording.STATUS_READY,
                started_at=started_at,
                ended_at=now,
            )
            meeting.is_recording = False
            meeting.recording_started_by = None
            meeting.recording_started_at = None
            meeting.save(update_fields=["is_recording", "recording_started_by", "recording_started_at"])
            ActivityLog.objects.create(
                user=request.user,
                action="recording_saved",
                detail=f"{meeting.meeting_code}: {rec.title} ({rec.file_size} bytes)",
            )
        return Response(MeetingRecordingSerializer(rec).data, status=status.HTTP_201_CREATED)


class RecordingDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, recording_id):
        recording = get_object_or_404(MeetingRecording.objects.select_related("meeting"), id=recording_id)
        if not is_meeting_teacher(request.user, recording.meeting):
            return Response(
                {"detail": "Only the meeting teacher can modify this recording."},
                status=status.HTTP_403_FORBIDDEN,
            )
        updated = []
        if "title" in request.data:
            new_title = str(request.data.get("title") or "").strip()
            if len(new_title) >= 2:
                recording.title = new_title[:160]
                updated.append("title")
        if "visibility" in request.data:
            vis = str(request.data.get("visibility") or "").strip()
            if vis not in {c[0] for c in MeetingRecording.VISIBILITY_CHOICES}:
                return Response({"detail": "Invalid visibility setting."}, status=status.HTTP_400_BAD_REQUEST)
            recording.visibility = vis
            updated.append("visibility")
        if updated:
            recording.save(update_fields=updated)
        return Response(MeetingRecordingSerializer(recording).data)

    def delete(self, request, recording_id):
        recording = get_object_or_404(MeetingRecording.objects.select_related("meeting"), id=recording_id)
        if not is_meeting_teacher(request.user, recording.meeting):
            return Response(
                {"detail": "Only the meeting teacher can delete this recording."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if recording.file and recording.file.storage.exists(recording.file.name):
            recording.file.delete(save=False)
        recording.delete()
        ActivityLog.objects.create(
            user=request.user,
            action="recording_deleted",
            detail=f"{recording.meeting.meeting_code}: {recording.title}",
        )
        return Response({"detail": "Recording deleted."})


class RecordingDownloadView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, recording_id):
        recording = get_object_or_404(MeetingRecording.objects.select_related("meeting"), id=recording_id)
        if not can_access_recording(request.user, recording):
            return Response(
                {"detail": "You are not authorized to access this recording."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not recording.file or not recording.file.storage.exists(recording.file.name):
            return Response({"detail": "Recording file is unavailable."}, status=status.HTTP_404_NOT_FOUND)

        as_inline = request.query_params.get("inline") in ("1", "true")
        return FileResponse(
            recording.file.open("rb"),
            as_attachment=not as_inline,
            filename=recording.original_name,
            content_type=recording.mime_type or "video/webm",
        )


class MeetingLeaveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_code):
        now = timezone.now()
        with transaction.atomic():
            meeting = get_object_or_404(Meeting.objects.select_for_update(), meeting_code=meeting_code.lower())
            MeetingParticipant.objects.filter(meeting=meeting, user=request.user).update(
                is_active=False,
                left_at=now,
                hand_raised=False,
                current_breakout=None,
            )
            close_user_attendance_sessions(meeting, request.user.id, at_time=now)
            if meeting.active_screen_sharer_id == request.user.id:
                meeting.active_screen_sharer = None
                meeting.active_screen_sharer_sid = ""
                meeting.save(update_fields=["active_screen_sharer", "active_screen_sharer_sid"])
            ActivityLog.objects.create(
                user=request.user,
                action="meeting_left",
                detail=meeting.meeting_code,
            )
        return Response({"detail": "Left meeting."})


class MeetingEndView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_code):
        with transaction.atomic():
            meeting = get_object_or_404(Meeting.objects.select_for_update(), meeting_code=meeting_code.lower())
            if not is_meeting_teacher(request.user, meeting) and not is_platform_admin(request.user):
                return Response(
                    {"detail": "Only the meeting teacher or an administrator can end this meeting."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            now = timezone.now()
            meeting.status = Meeting.STATUS_ENDED
            meeting.ended_at = now
            meeting.active_screen_sharer = None
            meeting.active_screen_sharer_sid = ""
            meeting.is_recording = False
            meeting.recording_started_by = None
            meeting.recording_started_at = None
            meeting.save(
                update_fields=[
                    "status",
                    "ended_at",
                    "active_screen_sharer",
                    "active_screen_sharer_sid",
                    "is_recording",
                    "recording_started_by",
                    "recording_started_at",
                ]
            )
            MeetingParticipant.objects.filter(meeting=meeting, is_active=True).update(
                is_active=False,
                left_at=now,
                hand_raised=False,
                current_breakout=None,
            )
            MeetingParticipant.objects.filter(meeting=meeting).update(current_breakout=None, hand_raised=False)
            BreakoutRoom.objects.filter(meeting=meeting).exclude(status=BreakoutRoom.STATUS_CLOSED).update(
                status=BreakoutRoom.STATUS_CLOSED,
                closed_at=now,
            )
            close_all_attendance_sessions(meeting, at_time=now)
            ActivityLog.objects.create(
                user=request.user,
                action="meeting_ended",
                detail=meeting.meeting_code,
            )
        logger.info("Meeting %s ended by %s", meeting.meeting_code, request.user.username)
        return Response({"detail": "Meeting ended.", "status": meeting.status})


class MeetingIceServersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Not a member of this meeting."}, status=status.HTTP_403_FORBIDDEN)

        ice_servers = [{"urls": url} for url in settings.STUN_URLS]
        if settings.TURN_URL:
            turn_entry = {"urls": settings.TURN_URL}
            if settings.TURN_USERNAME:
                turn_entry["username"] = settings.TURN_USERNAME
            if settings.TURN_CREDENTIAL:
                turn_entry["credential"] = settings.TURN_CREDENTIAL
            ice_servers.append(turn_entry)

        return Response({"ice_servers": ice_servers})
