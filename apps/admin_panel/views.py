import logging
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdminRole
from apps.accounts.serializers import UserSerializer
from apps.fileshare.models import SharedFile
from apps.fileshare.serializers import SharedFileSerializer
from apps.meetings.models import ActivityLog, Meeting
from apps.meetings.serializers import ActivityLogSerializer, MeetingSerializer

User = get_user_model()
logger = logging.getLogger("apps.admin_panel")


class AdminOverviewView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        users = User.objects.all().order_by("-date_joined")
        meetings = Meeting.objects.all().select_related("host")[:100]
        files = SharedFile.objects.all().select_related("meeting", "uploaded_by")[:100]
        activity = ActivityLog.objects.all().select_related("user")[:50]

        stats = {
            "total_users": User.objects.count(),
            "active_users": User.objects.filter(is_active=True).count(),
            "total_meetings": Meeting.objects.count(),
            "active_meetings": Meeting.objects.filter(status=Meeting.STATUS_ACTIVE).count(),
            "total_files": SharedFile.objects.count(),
        }

        return Response(
            {
                "stats": stats,
                "users": UserSerializer(users, many=True).data,
                "meetings": MeetingSerializer(meetings, many=True, context={"request": request}).data,
                "files": SharedFileSerializer(files, many=True).data,
                "activity": ActivityLogSerializer(activity, many=True).data,
            }
        )


class AdminUserStatusView(APIView):
    permission_classes = [IsAdminRole]

    def patch(self, request, user_id):
        target_user = get_object_or_404(User, id=user_id)
        if target_user.id == request.user.id:
            return Response(
                {"detail": "You cannot deactivate your own administrator account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        is_active = request.data.get("is_active")
        if not isinstance(is_active, bool):
            return Response(
                {"detail": "Boolean field 'is_active' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_user.is_active = is_active
        target_user.save(update_fields=["is_active"])
        ActivityLog.objects.create(
            user=request.user,
            action="admin_user_status",
            detail=f"@{target_user.username} active={is_active}",
        )
        logger.info("Admin %s set user %s active=%s", request.user.username, target_user.username, is_active)
        return Response(UserSerializer(target_user).data)
