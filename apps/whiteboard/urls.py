from django.urls import path
from .views import WhiteboardStateView

urlpatterns = [
    path("meetings/<str:meeting_code>/whiteboard/", WhiteboardStateView.as_view(), name="meeting-whiteboard"),
]
