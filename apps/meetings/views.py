import logging
from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.fileshare.models import SharedFile
from apps.fileshare.serializers import SharedFileSerializer
from .models import ActivityLog, Meeting, MeetingParticipant
from .permissions import is_meeting_member
from .serializers import MeetingCreateSerializer, MeetingParticipantSerializer, MeetingSerializer

logger = logging.getLogger("apps.meetings")


class DashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        meetings = (
            Meeting.objects.filter(Q(host=user) | Q(participants__user=user))
            .distinct()
            .select_related("host")
        )
        recent_files = (
            SharedFile.objects.filter(Q(meeting__host=user) | Q(meeting__participants__user=user))
            .distinct()
            .select_related("meeting", "uploaded_by")[:10]
        )

        stats = {
            "total_meetings": meetings.count(),
            "active_meetings": meetings.filter(status=Meeting.STATUS_ACTIVE).count(),
            "hosted_meetings": meetings.filter(host=user).count(),
            "shared_files": SharedFile.objects.filter(
                Q(meeting__host=user) | Q(meeting__participants__user=user)
            ).distinct().count(),
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
            Meeting.objects.filter(Q(host=user) | Q(participants__user=user))
            .distinct()
            .select_related("host")
        )
        return Response(MeetingSerializer(meetings, many=True, context={"request": request}).data)

    def post(self, request):
        serializer = MeetingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scheduled_at = serializer.validated_data.get("scheduled_at")
        initial_status = (
            Meeting.STATUS_SCHEDULED
            if scheduled_at and scheduled_at > timezone.now()
            else Meeting.STATUS_ACTIVE
        )
        meeting = Meeting.objects.create(
            title=serializer.validated_data["title"].strip(),
            host=request.user,
            status=initial_status,
            scheduled_at=scheduled_at,
        )
        MeetingParticipant.objects.create(
            meeting=meeting,
            user=request.user,
            role=MeetingParticipant.ROLE_HOST,
            is_active=True,
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
        meeting = get_object_or_404(Meeting.objects.select_related("host"), meeting_code=meeting_code.lower())
        return Response(MeetingSerializer(meeting, context={"request": request}).data)


class MeetingJoinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_code):
        meeting = get_object_or_404(Meeting.objects.select_related("host"), meeting_code=meeting_code.lower())
        if meeting.status == Meeting.STATUS_ENDED:
            return Response(
                {"detail": "This meeting has already ended."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if meeting.status == Meeting.STATUS_SCHEDULED:
            meeting.status = Meeting.STATUS_ACTIVE
            meeting.save(update_fields=["status"])

        role = MeetingParticipant.ROLE_HOST if meeting.host_id == request.user.id else MeetingParticipant.ROLE_PARTICIPANT
        participant, created = MeetingParticipant.objects.get_or_create(
            meeting=meeting,
            user=request.user,
            defaults={"role": role, "is_active": True},
        )
        if not created:
            participant.is_active = True
            participant.left_at = None
            participant.role = role
            participant.save(update_fields=["is_active", "left_at", "role"])

        ActivityLog.objects.create(
            user=request.user,
            action="meeting_joined",
            detail=meeting.meeting_code,
        )
        data = MeetingSerializer(meeting, context={"request": request}).data
        data["participants"] = MeetingParticipantSerializer(
            meeting.participants.filter(is_active=True).select_related("user"),
            many=True,
        ).data
        return Response(data)


class MeetingLeaveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        MeetingParticipant.objects.filter(meeting=meeting, user=request.user).update(
            is_active=False,
            left_at=timezone.now(),
        )
        ActivityLog.objects.create(
            user=request.user,
            action="meeting_left",
            detail=meeting.meeting_code,
        )
        return Response({"detail": "Left meeting."})


class MeetingEndView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        is_admin = request.user.is_staff or request.user.role == "admin"
        if meeting.host_id != request.user.id and not is_admin:
            return Response(
                {"detail": "Only the meeting host or an administrator can end this meeting."},
                status=status.HTTP_403_FORBIDDEN,
            )

        now = timezone.now()
        meeting.status = Meeting.STATUS_ENDED
        meeting.ended_at = now
        meeting.save(update_fields=["status", "ended_at"])
        MeetingParticipant.objects.filter(meeting=meeting, is_active=True).update(
            is_active=False,
            left_at=now,
        )
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
