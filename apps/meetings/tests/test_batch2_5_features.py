import os
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.meetings.models import (
    AttendanceSession,
    BreakoutRoom,
    Meeting,
    MeetingChatMessage,
    MeetingParticipant,
    MeetingRecording,
)
from apps.whiteboard.models import WhiteboardOperation

User = get_user_model()


def _make_user(username, role="member", is_staff=False, full_name=""):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="Password123!",
        role=role,
        is_staff=is_staff,
        full_name=full_name or username,
    )


def _auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_batch2_moderation_file_share_and_eviction():
    teacher = _make_user("b2_tchr", full_name="Teacher Two")
    s1 = _make_user("b2_s1", full_name="Student A")
    s2 = _make_user("b2_s2", full_name="Student B")

    tc = _auth_client(teacher)
    s1c = _auth_client(s1)
    s2c = _auth_client(s2)

    code = tc.post("/api/v1/meetings/", {"title": "Batch 2 Class"}, format="json").data["meeting_code"]
    meeting = Meeting.objects.get(meeting_code=code)

    MeetingParticipant.objects.create(meeting=meeting, user=s1, role="student", admission_status="admitted", is_active=True)
    MeetingParticipant.objects.create(meeting=meeting, user=s2, role="student", admission_status="admitted", is_active=True)

    # Toggle Batch 2 permissions
    perm_res = tc.patch(
        f"/api/v1/meetings/{code}/permissions/",
        {
            "allow_student_mic": False,
            "allow_student_camera": False,
            "allow_student_file_share": False,
            "chat_mode": "teacher_only",
        },
        format="json",
    )
    assert perm_res.status_code == 200
    assert perm_res.data["allow_student_mic"] is False
    assert perm_res.data["allow_student_camera"] is False
    assert perm_res.data["allow_student_file_share"] is False
    assert perm_res.data["chat_mode"] == "teacher_only"

    # Student file upload blocked when allow_student_file_share is False; teacher allowed
    student_pdf = SimpleUploadedFile("notes.txt", b"hello notes", content_type="text/plain")
    assert s1c.post(f"/api/v1/meetings/{code}/files/", {"file": student_pdf}, format="multipart").status_code == 403

    teacher_pdf = SimpleUploadedFile("syllabus.txt", b"teacher file", content_type="text/plain")
    assert tc.post(f"/api/v1/meetings/{code}/files/", {"file": teacher_pdf}, format="multipart").status_code == 201

    tc.patch(f"/api/v1/meetings/{code}/permissions/", {"allow_student_file_share": True}, format="json")
    student_pdf2 = SimpleUploadedFile("hw.txt", b"homework", content_type="text/plain")
    assert s1c.post(f"/api/v1/meetings/{code}/files/", {"file": student_pdf2}, format="multipart").status_code == 201

    # Unauthorized student moderation rejected
    assert s1c.post(f"/api/v1/meetings/{code}/moderation/", {"action": "mute_all_mics"}, format="json").status_code == 403

    # Bulk mute/disable cam affects students only, not teacher
    assert tc.post(f"/api/v1/meetings/{code}/moderation/", {"action": "mute_all_mics"}, format="json").status_code == 200
    assert tc.post(f"/api/v1/meetings/{code}/moderation/", {"action": "disable_all_cams"}, format="json").status_code == 200
    p_s1 = MeetingParticipant.objects.get(meeting=meeting, user=s1)
    p_t = MeetingParticipant.objects.get(meeting=meeting, user=teacher)
    assert p_s1.mic_locked is True
    assert p_s1.camera_locked is True
    assert p_t.mic_locked is False

    # Per-student unlock mic, mute chat, and hand raise/lower
    assert tc.post(f"/api/v1/meetings/{code}/moderation/", {"action": "unlock_mic", "user_id": s1.id}, format="json").status_code == 200
    assert tc.post(f"/api/v1/meetings/{code}/moderation/", {"action": "mute_chat", "user_id": s1.id}, format="json").status_code == 200
    p_s1.hand_raised = True
    p_s1.save(update_fields=["hand_raised"])
    assert tc.post(f"/api/v1/meetings/{code}/moderation/", {"action": "lower_hand", "user_id": s1.id}, format="json").status_code == 200
    p_s1.refresh_from_db()
    assert p_s1.mic_locked is False
    assert p_s1.chat_muted is True
    assert p_s1.hand_raised is False

    # Teacher removes Student 2 -> closes session, blocks rejoin with 403 admission_removed
    AttendanceSession.objects.create(meeting=meeting, user=s2, joined_at=timezone.now() - timedelta(minutes=3))
    rem_res = tc.post(f"/api/v1/meetings/{code}/moderation/", {"action": "remove", "user_id": s2.id}, format="json")
    assert rem_res.status_code == 200
    p_s2 = MeetingParticipant.objects.get(meeting=meeting, user=s2)
    assert p_s2.admission_status == MeetingParticipant.ADMISSION_REMOVED
    assert p_s2.is_active is False
    assert AttendanceSession.objects.filter(meeting=meeting, user=s2, left_at__isnull=True).count() == 0

    rejoin = s2c.post(f"/api/v1/meetings/{code}/join/")
    assert rejoin.status_code == 403
    assert rejoin.data["code"] == "admission_removed"


@pytest.mark.django_db
def test_batch3_breakout_rooms_isolation_and_return_control():
    teacher = _make_user("b3_tchr")
    s1 = _make_user("b3_s1")
    s2 = _make_user("b3_s2")

    tc = _auth_client(teacher)
    s1c = _auth_client(s1)
    s2c = _auth_client(s2)

    code = tc.post("/api/v1/meetings/", {"title": "Breakout Lab"}, format="json").data["meeting_code"]
    meeting = Meeting.objects.get(meeting_code=code)
    MeetingParticipant.objects.create(meeting=meeting, user=s1, role="student", admission_status="admitted", is_active=True)
    MeetingParticipant.objects.create(meeting=meeting, user=s2, role="student", admission_status="admitted", is_active=True)

    # Create 2 named breakout rooms + 10 min duration
    create_res = tc.post(
        f"/api/v1/meetings/{code}/breakouts/",
        {"names": ["Alpha", "Beta"], "duration_minutes": 10},
        format="json",
    )
    assert create_res.status_code == 201
    rooms = create_res.data["rooms"]
    assert len(rooms) == 2
    r1_id, r2_id = rooms[0]["id"], rooms[1]["id"]

    # Manual assign s1 -> Room 1, s2 -> Room 2
    assert (
        tc.post(
            f"/api/v1/meetings/{code}/breakouts/action/",
            {"action": "assign", "breakout_id": r1_id, "user_ids": [s1.id]},
            format="json",
        ).status_code
        == 200
    )
    assert (
        tc.post(
            f"/api/v1/meetings/{code}/breakouts/action/",
            {"action": "assign", "breakout_id": r2_id, "user_ids": [s2.id]},
            format="json",
        ).status_code
        == 200
    )

    # Lock return to main, then open all
    assert (
        tc.post(
            f"/api/v1/meetings/{code}/breakouts/action/",
            {"action": "set_return_policy", "allow_return_to_main": False},
            format="json",
        ).status_code
        == 200
    )
    open_res = tc.post(
        f"/api/v1/meetings/{code}/breakouts/action/",
        {"action": "open_all", "duration_minutes": 15},
        format="json",
    )
    assert open_res.status_code == 200
    assert all(r["status"] == "open" and r["ends_at"] for r in open_res.data["rooms"])

    # Move s1 & s2 into their assigned rooms in DB (REST action + active room tracking)
    MeetingParticipant.objects.filter(meeting=meeting, user=s1).update(current_breakout_id=r1_id)
    MeetingParticipant.objects.filter(meeting=meeting, user=s2).update(current_breakout_id=r2_id)

    # Student 1 cannot return to main when allow_return_to_main is False
    ret_deny = s1c.post(
        f"/api/v1/meetings/{code}/breakouts/action/",
        {"action": "return_to_main"},
        format="json",
    )
    assert ret_deny.status_code == 403

    # Scoped chat isolation: s1 messages inside Room 1, s2 cannot read/write Room 1 chat
    MeetingChatMessage.objects.create(meeting=meeting, breakout_room_id=r1_id, sender=s1, sender_role="student", text="Room 1 secret")
    MeetingChatMessage.objects.create(meeting=meeting, breakout_room_id=r2_id, sender=s2, sender_role="student", text="Room 2 secret")
    WhiteboardOperation.objects.create(meeting=meeting, breakout_room_id=r1_id, user=s1, op_id="r1op", op_type="draw", payload={"id": "r1op"})

    s1_r1_chat = s1c.get(f"/api/v1/meetings/{code}/chat/?breakout_room_id={r1_id}")
    assert s1_r1_chat.status_code == 200
    assert [m["text"] for m in s1_r1_chat.data["messages"]] == ["Room 1 secret"]

    # s2 blocked from Room 1 chat and whiteboard
    assert s2c.get(f"/api/v1/meetings/{code}/chat/?breakout_room_id={r1_id}").status_code == 403
    assert s2c.get(f"/api/v1/meetings/{code}/whiteboard/?breakout_room_id={r1_id}").status_code == 403
    assert tc.get(f"/api/v1/meetings/{code}/whiteboard/?breakout_room_id={r1_id}").status_code == 200

    # Student 1 can return once allow_return_to_main is True
    assert (
        tc.post(
            f"/api/v1/meetings/{code}/breakouts/action/",
            {"action": "set_return_policy", "allow_return_to_main": True},
            format="json",
        ).status_code
        == 200
    )
    assert s1c.post(f"/api/v1/meetings/{code}/breakouts/action/", {"action": "return_to_main"}, format="json").status_code == 200

    # Close all rooms resets status and clears current_breakout
    close_res = tc.post(f"/api/v1/meetings/{code}/breakouts/action/", {"action": "close_all"}, format="json")
    assert close_res.status_code == 200
    assert all(r["status"] == "closed" for r in close_res.data["rooms"])
    assert MeetingParticipant.objects.get(meeting=meeting, user=s2).current_breakout_id is None


@pytest.mark.django_db
def test_batch4_attendance_sessions_late_override_and_safe_csv():
    teacher = _make_user("b4_tchr", full_name="=CMD('calc')")  # Test CSV formula injection escaping
    s1 = _make_user("b4_s1", full_name="OnTime Student")
    s2 = _make_user("b4_s2", full_name="+Late Student")

    tc = _auth_client(teacher)
    s1c = _auth_client(s1)

    now = timezone.now()
    scheduled = now - timedelta(minutes=20)
    meeting = Meeting.objects.create(title="Attendance Lecture", host=teacher, scheduled_at=scheduled)
    MeetingParticipant.objects.create(meeting=meeting, user=teacher, role="teacher", admission_status="admitted", is_active=True)
    MeetingParticipant.objects.create(
        meeting=meeting, user=s1, role="student", admission_status="admitted", is_active=True, admitted_at=scheduled + timedelta(minutes=1)
    )
    MeetingParticipant.objects.create(
        meeting=meeting, user=s2, role="student", admission_status="admitted", is_active=True, admitted_at=scheduled + timedelta(minutes=12)
    )

    # S1: two completed intervals (120s + 60s = 180s), first_joined within 5 min -> present
    AttendanceSession.objects.create(
        meeting=meeting,
        user=s1,
        joined_at=scheduled + timedelta(minutes=1),
        left_at=scheduled + timedelta(minutes=3),
        duration_seconds=120,
    )
    AttendanceSession.objects.create(
        meeting=meeting,
        user=s1,
        joined_at=scheduled + timedelta(minutes=5),
        left_at=scheduled + timedelta(minutes=6),
        duration_seconds=60,
    )
    # S2: joined 12 min after scheduled_at -> late
    AttendanceSession.objects.create(
        meeting=meeting,
        user=s2,
        joined_at=scheduled + timedelta(minutes=12),
        left_at=scheduled + timedelta(minutes=15),
        duration_seconds=180,
    )

    # Student sees only self and cannot override/export
    s_rep = s1c.get(f"/api/v1/meetings/{meeting.meeting_code}/attendance/")
    assert s_rep.status_code == 200
    assert len(s_rep.data["records"]) == 1
    assert s_rep.data["records"][0]["user_id"] == s1.id
    assert s_rep.data["records"][0]["join_count"] == 2
    assert s_rep.data["records"][0]["total_duration_seconds"] == 180
    assert s_rep.data["records"][0]["status"] == "present"
    assert s_rep.data["can_export"] is False
    assert (
        s1c.patch(
            f"/api/v1/meetings/{meeting.meeting_code}/attendance/",
            {"user_id": s2.id, "status_override": "absent"},
            format="json",
        ).status_code
        == 403
    )
    assert s1c.get(f"/api/v1/meetings/{meeting.meeting_code}/attendance/export/").status_code == 403

    # Teacher sees all records including late s2
    t_rep = tc.get(f"/api/v1/meetings/{meeting.meeting_code}/attendance/")
    assert t_rep.status_code == 200
    by_uid = {r["user_id"]: r for r in t_rep.data["records"]}
    assert by_uid[s2.id]["status"] == "late"

    # Teacher overrides s2 to excused + note
    patch_res = tc.patch(
        f"/api/v1/meetings/{meeting.meeting_code}/attendance/",
        {"user_id": s2.id, "status_override": "excused", "note": "@Approved medical excuse"},
        format="json",
    )
    assert patch_res.status_code == 200
    p_by_uid = {r["user_id"]: r for r in patch_res.data["records"]}
    assert p_by_uid[s2.id]["status"] == "excused"
    assert p_by_uid[s2.id]["note"] == "@Approved medical excuse"

    # CSV export includes headers and formula-injection escaping
    csv_res = tc.get(f"/api/v1/meetings/{meeting.meeting_code}/attendance/export/")
    assert csv_res.status_code == 200
    assert "text/csv" in csv_res["Content-Type"]
    csv_text = csv_res.content.decode("utf-8")
    assert "Meeting Code,Name,Username,Email" in csv_text
    assert "'=CMD('calc')" in csv_text
    assert "'+Late Student" in csv_text
    assert "'@Approved medical excuse" in csv_text


@pytest.mark.django_db
def test_batch5_recordings_upload_visibility_download_and_delete(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path), MAX_RECORDING_SIZE_BYTES=1024 * 1024, MAX_RECORDING_SIZE_MB=1):
        teacher = _make_user("b5_tchr")
        student = _make_user("b5_stud")
        outsider = _make_user("b5_out")

        tc = _auth_client(teacher)
        sc = _auth_client(student)
        oc = _auth_client(outsider)

        code = tc.post("/api/v1/meetings/", {"title": "Recorded Seminar"}, format="json").data["meeting_code"]
        meeting = Meeting.objects.get(meeting_code=code)
        MeetingParticipant.objects.create(
            meeting=meeting, user=student, role="student", admission_status="admitted", is_active=True
        )

        # Student cannot upload recording
        s_vid = SimpleUploadedFile("s.webm", b"WEBM_DATA", content_type="video/webm")
        assert sc.post(f"/api/v1/meetings/{code}/recordings/", {"file": s_vid}, format="multipart").status_code == 403

        # Reject invalid extension & empty file
        bad_ext = SimpleUploadedFile("bad.exe", b"MZ1234", content_type="application/octet-stream")
        assert tc.post(f"/api/v1/meetings/{code}/recordings/", {"file": bad_ext}, format="multipart").status_code == 400
        empty_vid = SimpleUploadedFile("empty.webm", b"", content_type="video/webm")
        assert tc.post(f"/api/v1/meetings/{code}/recordings/", {"file": empty_vid}, format="multipart").status_code == 400

        # Valid upload defaults to teacher_only
        good_vid = SimpleUploadedFile("lecture.webm", b"\x1a\x45\xdf\xa3recording-bytes", content_type="video/webm")
        up_res = tc.post(
            f"/api/v1/meetings/{code}/recordings/",
            {"file": good_vid, "title": "Lecture 1", "visibility": "teacher_only", "duration_seconds": "42"},
            format="multipart",
        )
        assert up_res.status_code == 201
        rec_id = up_res.data["id"]
        rec_obj = MeetingRecording.objects.get(id=rec_id)
        stored_path = rec_obj.file.path
        assert os.path.exists(stored_path)

        # Student cannot see or download teacher_only recording
        assert len(sc.get(f"/api/v1/meetings/{code}/recordings/").data["recordings"]) == 0
        assert sc.get(f"/api/v1/meetings/recordings/{rec_id}/download/").status_code == 403

        # Student cannot change visibility
        assert sc.patch(f"/api/v1/meetings/recordings/{rec_id}/", {"visibility": "participants"}, format="json").status_code == 403

        # Teacher shares recording with participants -> student can list & download, outsider still 403
        assert tc.patch(f"/api/v1/meetings/recordings/{rec_id}/", {"visibility": "participants"}, format="json").status_code == 200
        assert len(sc.get(f"/api/v1/meetings/{code}/recordings/").data["recordings"]) == 1
        dl_res = sc.get(f"/api/v1/meetings/recordings/{rec_id}/download/")
        assert dl_res.status_code == 200
        assert b"".join(dl_res.streaming_content) == b"\x1a\x45\xdf\xa3recording-bytes"
        assert oc.get(f"/api/v1/meetings/recordings/{rec_id}/download/").status_code == 403

        # End meeting resets active recording flag cleanly
        meeting.is_recording = True
        meeting.recording_started_by = teacher
        meeting.save(update_fields=["is_recording", "recording_started_by"])
        assert tc.post(f"/api/v1/meetings/{code}/end/").status_code == 200
        meeting.refresh_from_db()
        assert meeting.is_recording is False

        # Teacher delete removes DB record and file on disk
        assert tc.delete(f"/api/v1/meetings/recordings/{rec_id}/").status_code == 200
        assert not MeetingRecording.objects.filter(id=rec_id).exists()
        assert not os.path.exists(stored_path)


@pytest.mark.django_db
def test_recording_upload_retries_are_idempotent(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path), MAX_RECORDING_SIZE_BYTES=1024 * 1024, MAX_RECORDING_SIZE_MB=1):
        teacher = _make_user("rec_retry_teacher")
        client = _auth_client(teacher)
        code = client.post("/api/v1/meetings/", {"title": "Retry recording"}, format="json").data["meeting_code"]
        upload_id = str(uuid.uuid4())
        filename = f"convia-{code.lower()}-{upload_id}.webm"
        payload = {
            "title": "Retry recording",
            "visibility": "teacher_only",
            "duration_seconds": "18",
            "upload_id": upload_id,
        }

        first = client.post(
            f"/api/v1/meetings/{code}/recordings/",
            {**payload, "file": SimpleUploadedFile(filename, b"recording-data", content_type="video/webm")},
            format="multipart",
        )
        retry = client.post(
            f"/api/v1/meetings/{code}/recordings/",
            {**payload, "file": SimpleUploadedFile(filename, b"recording-data", content_type="video/webm")},
            format="multipart",
        )

        assert first.status_code == 201
        assert retry.status_code == 200
        assert retry.data["id"] == first.data["id"]
        assert MeetingRecording.objects.filter(meeting__meeting_code=code).count() == 1

        conflict = client.post(
            f"/api/v1/meetings/{code}/recordings/",
            {**payload, "file": SimpleUploadedFile(filename, b"different-payload", content_type="video/webm")},
            format="multipart",
        )
        assert conflict.status_code == 409
