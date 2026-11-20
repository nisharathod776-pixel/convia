from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.meetings.models import Meeting, MeetingChatMessage, MeetingParticipant
from apps.whiteboard.models import WhiteboardOperation

User = get_user_model()


@pytest.mark.django_db
def test_platform_admin_not_treated_as_teacher_in_other_meeting():
    teacher = User.objects.create_user(username="tchr1", email="tchr1@example.com", password="Password123!")
    admin_user = User.objects.create_user(
        username="sysadmin1",
        email="sysadmin1@example.com",
        password="Password123!",
        role="admin",
        is_staff=True,
    )

    teacher_client = APIClient()
    teacher_client.force_authenticate(user=teacher)

    admin_client = APIClient()
    admin_client.force_authenticate(user=admin_user)

    res = teacher_client.post("/api/v1/meetings/", {"title": "Physics 101"}, format="json")
    assert res.status_code == 201
    code = res.data["meeting_code"]
    assert res.data["meeting_role"] == "teacher"

    # A platform admin joining another user's meeting is admitted as a student.
    join_res = admin_client.post(f"/api/v1/meetings/{code}/join/")
    assert join_res.status_code == 200
    assert join_res.data["admission_status"] == "admitted"
    assert join_res.data["meeting_role"] == "student"
    assert join_res.data["is_host"] is False

    # Admin cannot view waiting room or approve themselves
    assert admin_client.get(f"/api/v1/meetings/{code}/admissions/").status_code == 403
    assert (
        admin_client.post(
            f"/api/v1/meetings/{code}/admissions/",
            {"user_id": admin_user.id, "decision": "admit"},
            format="json",
        ).status_code
        == 403
    )
    assert (
        admin_client.patch(
            f"/api/v1/meetings/{code}/permissions/",
            {"whiteboard_student_write_enabled": True},
            format="json",
        ).status_code
        == 403
    )
    assert admin_client.delete(f"/api/v1/meetings/{code}/whiteboard/").status_code == 403


@pytest.mark.django_db
def test_scheduled_join_window_and_admission_deadline():
    teacher = User.objects.create_user(username="tchr_win", email="twin@example.com", password="Password123!")
    student_early = User.objects.create_user(username="stud_early", email="searly@example.com", password="Password123!")
    student_late = User.objects.create_user(username="stud_late", email="slate@example.com", password="Password123!")

    t_client = APIClient()
    t_client.force_authenticate(user=teacher)

    now = timezone.now()
    # Invalid: deadline before scheduled start
    bad_res = t_client.post(
        "/api/v1/meetings/",
        {
            "title": "Invalid Window",
            "scheduled_at": (now + timedelta(hours=2)).isoformat(),
            "admission_deadline": (now + timedelta(hours=1)).isoformat(),
        },
        format="json",
    )
    assert bad_res.status_code == 400

    # Future scheduled meeting rejects early join
    future_res = t_client.post(
        "/api/v1/meetings/",
        {
            "title": "Future Class",
            "scheduled_at": (now + timedelta(hours=1)).isoformat(),
            "admission_deadline": (now + timedelta(hours=2)).isoformat(),
        },
        format="json",
    )
    assert future_res.status_code == 201
    future_code = future_res.data["meeting_code"]

    early_client = APIClient()
    early_client.force_authenticate(user=student_early)
    not_started = early_client.post(f"/api/v1/meetings/{future_code}/join/")
    assert not_started.status_code == 403
    assert not_started.data["code"] == "not_started"

    # Active meeting with open admission window
    open_res = t_client.post(
        "/api/v1/meetings/",
        {
            "title": "Open Window Class",
            "admission_deadline": (now + timedelta(minutes=30)).isoformat(),
        },
        format="json",
    )
    assert open_res.status_code == 201
    open_code = open_res.data["meeting_code"]

    joined_res = early_client.post(f"/api/v1/meetings/{open_code}/join/")
    assert joined_res.status_code == 200
    assert joined_res.data["admission_status"] == "admitted"

    # Simulate deadline expiration now
    meeting_obj = Meeting.objects.get(meeting_code=open_code)
    meeting_obj.admission_deadline = timezone.now() - timedelta(minutes=1)
    meeting_obj.save(update_fields=["admission_deadline"])

    # New student blocked with exact required error message
    late_client = APIClient()
    late_client.force_authenticate(user=student_late)
    expired_res = late_client.post(f"/api/v1/meetings/{open_code}/join/")
    assert expired_res.status_code == 403
    assert expired_res.data["code"] == "admission_expired"
    assert expired_res.data["detail"] == "The meeting admission time has expired. Please contact your teacher."

    # Already admitted student remains able to access and stay in the meeting
    reenter_res = early_client.post(f"/api/v1/meetings/{open_code}/join/")
    assert reenter_res.status_code == 200
    assert early_client.get(f"/api/v1/meetings/{open_code}/ice-servers/").status_code == 200

    # Teacher can still re-enter after admission deadline
    assert t_client.post(f"/api/v1/meetings/{open_code}/join/").status_code == 200


@pytest.mark.django_db
def test_join_auto_admits_guests_even_when_waiting_room_is_enabled():
    teacher = User.objects.create_user(username="tchr_wr", email="twr@example.com", password="Password123!")
    s1 = User.objects.create_user(username="stud_wr1", email="swr1@example.com", password="Password123!")

    t_client = APIClient()
    t_client.force_authenticate(user=teacher)
    s_client = APIClient()
    s_client.force_authenticate(user=s1)

    code = t_client.post(
        "/api/v1/meetings/",
        {"title": "Automatic Admission Check", "waiting_room_enabled": True},
        format="json",
    ).data["meeting_code"]

    # Guests are admitted directly and repeated joins reuse one membership.
    r1 = s_client.post(f"/api/v1/meetings/{code}/join/")
    r2 = s_client.post(f"/api/v1/meetings/{code}/join/")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.data["admission_status"] == "admitted"
    assert MeetingParticipant.objects.filter(meeting__meeting_code=code, user=s1).count() == 1

    participant = MeetingParticipant.objects.get(meeting__meeting_code=code, user=s1)
    assert participant.admission_status == MeetingParticipant.ADMISSION_ADMITTED
    assert s_client.get(f"/api/v1/meetings/{code}/chat/").status_code == 200

    # Student cannot reject/admit themselves
    assert (
        s_client.post(
            f"/api/v1/meetings/{code}/admissions/",
            {"user_id": s1.id, "decision": "admit"},
            format="json",
        ).status_code
        == 403
    )

    # No admission request is pending for the teacher to approve or reject.
    stale = t_client.post(
        f"/api/v1/meetings/{code}/admissions/",
        {"user_id": s1.id, "decision": "reject"},
        format="json",
    )
    assert stale.status_code == 400

    # The guest remains admitted on subsequent joins.
    rejoin = s_client.post(f"/api/v1/meetings/{code}/join/")
    assert rejoin.status_code == 200
    assert rejoin.data["admission_status"] == "admitted"


@pytest.mark.django_db
def test_chat_deletion_and_whiteboard_clear_rbac():
    teacher = User.objects.create_user(username="tchr_rbac", email="trbac@example.com", password="Password123!")
    s1 = User.objects.create_user(username="stud_rbac1", email="srbac1@example.com", password="Password123!")
    s2 = User.objects.create_user(username="stud_rbac2", email="srbac2@example.com", password="Password123!")

    t_client = APIClient()
    t_client.force_authenticate(user=teacher)
    s1_client = APIClient()
    s1_client.force_authenticate(user=s1)
    s2_client = APIClient()
    s2_client.force_authenticate(user=s2)

    code = t_client.post("/api/v1/meetings/", {"title": "Moderation Room"}, format="json").data["meeting_code"]
    meeting = Meeting.objects.get(meeting_code=code)

    MeetingParticipant.objects.create(meeting=meeting, user=s1, role="student", admission_status="admitted", is_active=True)
    MeetingParticipant.objects.create(meeting=meeting, user=s2, role="student", admission_status="admitted", is_active=True)

    WhiteboardOperation.objects.create(
        meeting=meeting, user=teacher, op_id="op1", op_type="draw", payload={"id": "op1", "type": "draw"}
    )

    # Even if whiteboard_student_write_enabled is turned on, only teacher can clear board
    t_client.patch(
        f"/api/v1/meetings/{code}/permissions/",
        {"whiteboard_student_write_enabled": True},
        format="json",
    )
    assert s1_client.delete(f"/api/v1/meetings/{code}/whiteboard/").status_code == 403
    assert WhiteboardOperation.objects.filter(meeting=meeting).count() == 1
    assert t_client.delete(f"/api/v1/meetings/{code}/whiteboard/").status_code == 200
    assert WhiteboardOperation.objects.filter(meeting=meeting).count() == 0

    msg_teacher = MeetingChatMessage.objects.create(
        meeting=meeting, sender=teacher, sender_role="teacher", text="Teacher syllabus https://example.com"
    )
    msg_s1 = MeetingChatMessage.objects.create(
        meeting=meeting, sender=s1, sender_role="student", text="Student 1 message"
    )
    msg_s2 = MeetingChatMessage.objects.create(
        meeting=meeting, sender=s2, sender_role="student", text="Student 2 message"
    )

    # Student cannot delete teacher message or another student's message
    assert s1_client.delete(f"/api/v1/meetings/{code}/chat/{msg_teacher.id}/").status_code == 403
    assert s1_client.delete(f"/api/v1/meetings/{code}/chat/{msg_s2.id}/").status_code == 403

    # Student can delete own student message
    assert s1_client.delete(f"/api/v1/meetings/{code}/chat/{msg_s1.id}/").status_code == 200
    msg_s1.refresh_from_db()
    assert msg_s1.is_deleted is True

    # Teacher can moderate student 2's message and delete their own
    assert t_client.delete(f"/api/v1/meetings/{code}/chat/{msg_s2.id}/").status_code == 200
    assert t_client.delete(f"/api/v1/meetings/{code}/chat/{msg_teacher.id}/").status_code == 200
    msg_s2.refresh_from_db()
    msg_teacher.refresh_from_db()
    assert msg_s2.is_deleted is True
    assert msg_teacher.is_deleted is True
