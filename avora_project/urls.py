from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from avora_project import page_views


def health_check(request):
    return JsonResponse({"status": "ok", "service": "avora"})


urlpatterns = [
    path("", page_views.landing_view, name="landing"),
    path("login/", page_views.login_view, name="login"),
    path("register/", page_views.register_view, name="register"),
    path("dashboard/", page_views.dashboard_view, name="dashboard"),
    path("meeting/<str:meeting_code>/", page_views.meeting_room_view, name="meeting-room"),
    path("profile/", page_views.profile_view, name="profile"),
    path("admin-panel/", page_views.admin_panel_view, name="admin-panel"),
    path("django-admin/", admin.site.urls),
    path("api/v1/health/", health_check, name="health-check"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/meetings/", include("apps.meetings.urls")),
    path("api/v1/", include("apps.fileshare.urls")),
    path("api/v1/", include("apps.whiteboard.urls")),
    path("api/v1/admin/", include("apps.admin_panel.urls")),
]
