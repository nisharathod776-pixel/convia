import logging
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.meetings.models import ActivityLog, Meeting
from apps.meetings.permissions import is_meeting_member
from .models import SharedFile
from .serializers import SharedFileSerializer
from .validators import validate_uploaded_file

logger = logging.getLogger("apps.fileshare")


class MeetingFilesView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Only meeting participants can access shared files."}, status=status.HTTP_403_FORBIDDEN)
        files = meeting.shared_files.select_related("meeting", "uploaded_by")
        return Response(SharedFileSerializer(files, many=True).data)

    def post(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Only meeting participants can upload files."}, status=status.HTTP_403_FORBIDDEN)
        if meeting.status == Meeting.STATUS_ENDED:
            return Response({"detail": "Cannot upload files to an ended meeting."}, status=status.HTTP_400_BAD_REQUEST)

        uploaded_file = request.FILES.get("file")
        safe_name, mime_type = validate_uploaded_file(uploaded_file)

        shared = SharedFile.objects.create(
            meeting=meeting,
            uploaded_by=request.user,
            file=uploaded_file,
            original_name=safe_name,
            file_size=uploaded_file.size,
            mime_type=mime_type,
        )
        ActivityLog.objects.create(
            user=request.user,
            action="file_uploaded",
            detail=f"{meeting.meeting_code}: {safe_name}",
        )
        logger.info("File %s uploaded to %s by %s", safe_name, meeting.meeting_code, request.user.username)
        return Response(SharedFileSerializer(shared).data, status=status.HTTP_201_CREATED)


class FileDownloadView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, file_id):
        shared = get_object_or_404(SharedFile.objects.select_related("meeting"), id=file_id)
        if not is_meeting_member(request.user, shared.meeting):
            return Response({"detail": "You are not authorized to download this file."}, status=status.HTTP_403_FORBIDDEN)

        if not shared.file or not shared.file.storage.exists(shared.file.name):
            return Response({"detail": "File is no longer available in storage."}, status=status.HTTP_404_NOT_FOUND)

        response = FileResponse(
            shared.file.open("rb"),
            as_attachment=True,
            filename=shared.original_name,
            content_type=shared.mime_type or "application/octet-stream",
        )
        return response
