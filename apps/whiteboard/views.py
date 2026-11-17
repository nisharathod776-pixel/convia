from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.meetings.models import BreakoutRoom, Meeting, MeetingParticipant
from apps.meetings.permissions import (
    can_clear_whiteboard,
    can_edit_whiteboard,
    is_meeting_member,
    is_meeting_teacher,
)
from .models import WhiteboardOperation


class WhiteboardStateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not is_meeting_member(request.user, meeting):
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)

        breakout_id = request.query_params.get("breakout_room_id")
        breakout_room = None
        if breakout_id:
            breakout_room = get_object_or_404(BreakoutRoom, id=breakout_id, meeting=meeting)
            if not is_meeting_teacher(request.user, meeting):
                is_assigned = MeetingParticipant.objects.filter(
                    meeting=meeting,
                    user=request.user,
                    admission_status=MeetingParticipant.ADMISSION_ADMITTED,
                    current_breakout=breakout_room,
                ).exists()
                if not is_assigned:
                    return Response(
                        {"detail": "Not authorized for this breakout room whiteboard."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            ops_qs = meeting.whiteboard_operations.filter(breakout_room=breakout_room)
        else:
            ops_qs = meeting.whiteboard_operations.filter(breakout_room__isnull=True)

        ops = [op.payload for op in ops_qs]
        return Response(
            {
                "meeting_code": meeting.meeting_code,
                "breakout_room_id": str(breakout_room.id) if breakout_room else None,
                "can_edit": can_edit_whiteboard(request.user, meeting, breakout_room=breakout_room),
                "can_clear": can_clear_whiteboard(request.user, meeting),
                "operations": ops,
            }
        )

    def delete(self, request, meeting_code):
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code.lower())
        if not can_clear_whiteboard(request.user, meeting):
            return Response(
                {"detail": "Only the meeting teacher can clear the whiteboard."},
                status=status.HTTP_403_FORBIDDEN,
            )
        breakout_id = request.query_params.get("breakout_room_id")
        if breakout_id:
            WhiteboardOperation.objects.filter(meeting=meeting, breakout_room_id=breakout_id).delete()
        else:
            WhiteboardOperation.objects.filter(meeting=meeting, breakout_room__isnull=True).delete()
        return Response({"detail": "Whiteboard cleared."})
