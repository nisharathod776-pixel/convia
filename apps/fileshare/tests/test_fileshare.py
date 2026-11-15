import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.meetings.models import Meeting, MeetingParticipant

User = get_user_model()


@pytest.mark.django_db
def test_file_upload_validation_and_authorized_download(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    host = User.objects.create_user(username="fhost", email="fhost@example.com", password="Password123!")
    outsider = User.objects.create_user(username="outsider", email="out@example.com", password="Password123!")

    meeting = Meeting.objects.create(title="File Room", host=host)
    MeetingParticipant.objects.create(meeting=meeting, user=host, role="host", is_active=True)

    host_client = APIClient()
    host_client.force_authenticate(user=host)
    outsider_client = APIClient()
    outsider_client.force_authenticate(user=outsider)

    # Disallowed executable extension rejected
    bad_file = SimpleUploadedFile("script.exe", b"MZ...", content_type="application/octet-stream")
    bad_res = host_client.post(
        f"/api/v1/meetings/{meeting.meeting_code}/files/",
        {"file": bad_file},
        format="multipart",
    )
    assert bad_res.status_code == 400

    # Valid PDF upload succeeds
    good_file = SimpleUploadedFile("spec.pdf", b"%PDF-1.4 test content", content_type="application/pdf")
    good_res = host_client.post(
        f"/api/v1/meetings/{meeting.meeting_code}/files/",
        {"file": good_file},
        format="multipart",
    )
    assert good_res.status_code == 201
    file_id = good_res.data["id"]

    # Outsider cannot list or download
    assert outsider_client.get(f"/api/v1/meetings/{meeting.meeting_code}/files/").status_code == 403
    assert outsider_client.get(f"/api/v1/files/{file_id}/download/").status_code == 403

    # Authorized host can download
    dl_res = host_client.get(f"/api/v1/files/{file_id}/download/")
    assert dl_res.status_code == 200
    assert b"%PDF-1.4" in b"".join(dl_res.streaming_content)
