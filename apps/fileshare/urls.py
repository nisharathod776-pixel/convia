from django.urls import path
from .views import FileDownloadView, MeetingFilesView

urlpatterns = [
    path("meetings/<str:meeting_code>/files/", MeetingFilesView.as_view(), name="meeting-files"),
    path("files/<uuid:file_id>/download/", FileDownloadView.as_view(), name="file-download"),
]
