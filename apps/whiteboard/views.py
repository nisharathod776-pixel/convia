from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.meetings.models import Meeting
from apps.meetings.permissions import is_meeting_member
from .models import WhiteboardOperation


class WhiteboardStateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)
        ops = [op.payload for op in meeting.whiteboard_operations.all()]
        return Response({"meeting_code": meeting.meeting_code, "operations": ops})

    def delete(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)
        WhiteboardOperation.objects.filter(meeting=meeting).delete()
        return Response({"detail": "Whiteboard cleared."})
