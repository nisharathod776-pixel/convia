from django.urls import path
from .views import (
    DashboardSummaryView,
    MeetingDetailView,
    MeetingEndView,
    MeetingIceServersView,
    MeetingJoinView,
    MeetingLeaveView,
    MeetingListCreateView,
)

urlpatterns = [
    path("", MeetingListCreateView.as_view(), name="meeting-list-create"),
    path("dashboard/", DashboardSummaryView.as_view(), name="meeting-dashboard"),
    path("<str:meeting_code>/", MeetingDetailView.as_view(), name="meeting-detail"),
    path("<str:meeting_code>/join/", MeetingJoinView.as_view(), name="meeting-join"),
    path("<str:meeting_code>/leave/", MeetingLeaveView.as_view(), name="meeting-leave"),
    path("<str:meeting_code>/end/", MeetingEndView.as_view(), name="meeting-end"),
    path("<str:meeting_code>/ice-servers/", MeetingIceServersView.as_view(), name="meeting-ice-servers"),
]
