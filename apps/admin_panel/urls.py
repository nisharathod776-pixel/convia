from django.urls import path
from .views import AdminOverviewView, AdminUserStatusView

urlpatterns = [
    path("overview/", AdminOverviewView.as_view(), name="admin-overview"),
    path("users/<int:user_id>/status/", AdminUserStatusView.as_view(), name="admin-user-status"),
]
