import asyncio
import pytest
from unittest.mock import AsyncMock
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta

from convia_project.settings import _configured_origins
from apps.meetings.models import BreakoutRoom, Meeting, MeetingParticipant
from apps.realtime.handlers_meeting import (
    _expire_breakouts_once,
    _breakout_action_db,
    _enter_meeting_and_load_state,
    _expire_due_breakouts_db,
)
from apps.realtime import handlers_meeting
from apps.realtime.validators import ROOM_PEERS
from apps.realtime.server import sio

User = get_user_model()


@pytest.mark.django_db
def test_expired_breakout_is_closed_before_reconnect_state_and_only_once():
    teacher = User.objects.create_user(
        username="expire_teacher",
        email="expire_teacher@example.com",
        password="Password123!",
    )
    student = User.objects.create_user(
        username="expire_student",
        email="expire_student@example.com",
        password="Password123!",
    )
    meeting = Meeting.objects.create(host=teacher, title="Expiry reconnect")
    room = BreakoutRoom.objects.create(
        meeting=meeting,
        name="Timed room",
        status=BreakoutRoom.STATUS_OPEN,
        ends_at=timezone.now() - timedelta(seconds=1),
    )
    MeetingParticipant.objects.create(
        meeting=meeting,
        user=student,
        role=MeetingParticipant.ROLE_STUDENT,
        admission_status=MeetingParticipant.ADMISSION_ADMITTED,
        current_breakout=room,
    )

    state = async_to_sync(_enter_meeting_and_load_state)(meeting.id, student.id, False)

    assert state[-1] is None
    assert state[4]["current_breakout_id"] is None
    room.refresh_from_db()
    assert room.status == BreakoutRoom.STATUS_CLOSED
    assert room.closed_at is not None
    assert BreakoutRoom.objects.filter(id=room.id, status=BreakoutRoom.STATUS_OPEN).count() == 0


@pytest.mark.django_db
def test_duplicate_breakout_expiry_actions_only_move_participants_once():
    teacher = User.objects.create_user(
        username="duplicate_expiry_teacher",
        email="duplicate_expiry_teacher@example.com",
        password="Password123!",
    )
    meeting = Meeting.objects.create(host=teacher, title="Duplicate expiry")
    room = BreakoutRoom.objects.create(
        meeting=meeting,
        name="Timed room",
        status=BreakoutRoom.STATUS_OPEN,
        ends_at=timezone.now() - timedelta(seconds=1),
    )

    first = async_to_sync(_breakout_action_db)(meeting.meeting_code, teacher.id, {"action": "expire"})
    second = async_to_sync(_breakout_action_db)(meeting.meeting_code, teacher.id, {"action": "expire"})

    assert first[0] is True
    assert first[3]["__expired_to_main__"] == [str(room.id)]
    assert second[0] is True
    assert second[3]["__expired_to_main__"] == []


@pytest.mark.django_db
def test_background_expiry_sweep_closes_rooms_without_client_actions():
    teacher = User.objects.create_user(
        username="scheduler_teacher",
        email="scheduler_teacher@example.com",
        password="Password123!",
    )
    student = User.objects.create_user(
        username="scheduler_student",
        email="scheduler_student@example.com",
        password="Password123!",
    )
    meeting = Meeting.objects.create(host=teacher, title="Scheduled expiry")
    room = BreakoutRoom.objects.create(
        meeting=meeting,
        name="Offline timed room",
        status=BreakoutRoom.STATUS_OPEN,
        ends_at=timezone.now() - timedelta(seconds=1),
    )
    participant = MeetingParticipant.objects.create(
        meeting=meeting,
        user=student,
        role=MeetingParticipant.ROLE_STUDENT,
        admission_status=MeetingParticipant.ADMISSION_ADMITTED,
        current_breakout=room,
    )

    updates = async_to_sync(_expire_due_breakouts_db)()

    room.refresh_from_db()
    participant.refresh_from_db()
    assert room.status == BreakoutRoom.STATUS_CLOSED
    assert participant.current_breakout_id is None
    assert any(
        update["meeting_code"] == meeting.meeting_code
        and str(room.id) in update["expired_ids"]
        for update in updates
    )


def test_scheduler_reconciles_local_peers_closed_by_another_worker(monkeypatch):
    meeting_code = "SCHEDULER-ROOM"
    breakout_id = "00000000-0000-0000-0000-000000000001"
    peers = {
        "socket-1": {
            "user_id": 1,
            "username": "participant",
            "breakout_room_id": breakout_id,
        }
    }
    ROOM_PEERS[meeting_code] = peers
    move_peer = AsyncMock()
    emit = AsyncMock()
    monkeypatch.setattr(handlers_meeting, "_expire_due_breakouts_db", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        handlers_meeting,
        "_closed_breakout_ids_db",
        AsyncMock(return_value={breakout_id}),
    )
    monkeypatch.setattr(
        handlers_meeting,
        "_breakout_rooms_for_meeting_db",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(handlers_meeting, "_move_peer_subchannel", move_peer)
    monkeypatch.setattr(sio, "emit", emit)
    try:
        async_to_sync(_expire_breakouts_once)()
    finally:
        ROOM_PEERS.pop(meeting_code, None)

    move_peer.assert_awaited_once_with(meeting_code, "socket-1", None)
    emit.assert_awaited_once()
    assert emit.await_args.args[0] == "breakout_state_updated"


def test_breakout_scheduler_start_stop_is_idempotent(monkeypatch):
    async def idle_loop():
        await asyncio.Event().wait()

    async def exercise_lifecycle():
        await handlers_meeting.start_breakout_expiry_scheduler()
        task = handlers_meeting._breakout_expiry_task
        await handlers_meeting.start_breakout_expiry_scheduler()
        assert handlers_meeting._breakout_expiry_task is task
        await handlers_meeting.stop_breakout_expiry_scheduler()
        assert task.cancelled()
        assert handlers_meeting._breakout_expiry_task is None

    monkeypatch.setattr(handlers_meeting, "_breakout_expiry_loop", idle_loop)
    async_to_sync(exercise_lifecycle)()


def test_socket_io_cors_uses_configured_allowed_origins():
    origins = sio.eio.cors_allowed_origins
    assert origins == settings.CORS_ALLOWED_ORIGINS
    assert "*" not in origins


def test_cors_configuration_rejects_wildcard_origins(monkeypatch):
    monkeypatch.setenv("AUDIT_TEST_CORS_ORIGINS", "http://localhost:3000,*")
    with pytest.raises(RuntimeError, match="wildcard origins are not allowed"):
        _configured_origins("AUDIT_TEST_CORS_ORIGINS", "http://localhost:3000")


def test_production_cors_configuration_requires_explicit_origins(monkeypatch):
    monkeypatch.delenv("AUDIT_TEST_CORS_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match="explicitly configured"):
        _configured_origins("AUDIT_TEST_CORS_ORIGINS", "")
