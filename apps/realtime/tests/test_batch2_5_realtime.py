import asyncio
import socket

import pytest
import socketio
import uvicorn
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

from apps.meetings.models import (
    AttendanceSession,
    BreakoutRoom,
    Meeting,
    MeetingChatMessage,
    MeetingParticipant,
)
from apps.whiteboard.models import WhiteboardOperation

User = get_user_model()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _create_user_and_token(username, full_name):
    user = await User.objects.acreate(
        username=username,
        email=f"{username}@example.com",
        full_name=full_name,
        role="member",
        is_active=True,
    )
    await sync_to_async(user.set_password)("Password123!")
    await sync_to_async(user.save)()
    refresh = await sync_to_async(RefreshToken.for_user)(user)
    return user, str(refresh.access_token)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_batch2_5_realtime_moderation_breakouts_attendance_and_recording():
    teacher, t_tok = await _create_user_and_token("b25_teacher", "Prof. Turing")
    s1, s1_tok = await _create_user_and_token("b25_stud1", "Student One")
    s2, s2_tok = await _create_user_and_token("b25_stud2", "Student Two")

    meeting = await Meeting.objects.acreate(
        title="Batch 2-5 Full Classroom",
        host=teacher,
        waiting_room_enabled=True,
        allow_student_mic=True,
        allow_student_camera=True,
        chat_mode="everyone",
    )
    await MeetingParticipant.objects.acreate(
        meeting=meeting, user=teacher, role="teacher", admission_status="admitted", is_active=True
    )
    await MeetingParticipant.objects.acreate(
        meeting=meeting, user=s1, role="student", admission_status="admitted", is_active=True
    )
    await MeetingParticipant.objects.acreate(
        meeting=meeting, user=s2, role="student", admission_status="admitted", is_active=True
    )

    port = _free_port()
    from avora_project.asgi import application

    config = uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(40):
        if server.started:
            break
        await asyncio.sleep(0.1)
    assert server.started

    url = f"http://127.0.0.1:{port}"
    c_t = socketio.AsyncClient()
    c_s1 = socketio.AsyncClient()
    c_s2 = socketio.AsyncClient()

    try:
        t_joined = asyncio.Event()
        s1_joined = asyncio.Event()
        s2_joined = asyncio.Event()

        s1_err_ev = asyncio.Event()
        s1_mod_ev = asyncio.Event()
        t_media_ev = asyncio.Event()
        s1_teacher_media_ev = asyncio.Event()
        joined_participants_ev = asyncio.Event()
        t_hand_ev = asyncio.Event()
        t_chat_ev = asyncio.Event()
        s1_chat_ev = asyncio.Event()
        s2_chat_ev = asyncio.Event()
        s1_wb_ev = asyncio.Event()
        s2_wb_ev = asyncio.Event()
        s2_offer_ev = asyncio.Event()

        s1_bo_entered_ev = asyncio.Event()
        s2_bo_entered_ev = asyncio.Event()
        s1_bo_exited_ev = asyncio.Event()
        s2_bo_exited_ev = asyncio.Event()
        s1_announce_ev = asyncio.Event()
        s1_rec_ev = asyncio.Event()
        s2_removed_ev = asyncio.Event()

        state = {
            "t_media": None,
            "t_media_events": [],
            "s1_teacher_media": None,
            "joined_participants": [],
            "t_hand": None,
            "t_chats": [],
            "s1_chats": [],
            "s2_chats": [],
            "s1_wbs": [],
            "s2_wbs": [],
            "s1_mod": None,
            "s1_rec": [],
            "s1_ann": None,
            "s2_offers": [],
        }

        @c_t.on("room_joined")
        async def on_t_j(d):
            t_joined.set()

        @c_s1.on("room_joined")
        async def on_s1_j(d):
            s1_joined.set()

        @c_s2.on("room_joined")
        async def on_s2_j(d):
            s2_joined.set()

        @c_t.on("participant_joined")
        async def on_t_participant_joined(d):
            state["joined_participants"].append(d["participant"])
            if len(state["joined_participants"]) == 2:
                joined_participants_ev.set()

        @c_s1.on("error_event")
        async def on_s1_err(d):
            s1_err_ev.set()

        @c_s1.on("moderation_enforced")
        async def on_s1_mod(d):
            state["s1_mod"] = d
            s1_mod_ev.set()

        @c_t.on("participant_media_state")
        async def on_t_media(d):
            state["t_media_events"].append(d)
            is_s1_sid = any(
                participant.get("user_id") == s1.id and participant.get("sid") == d.get("sid")
                for participant in state["joined_participants"]
            )
            if d.get("user_id") == s1.id or is_s1_sid:
                state["t_media"] = d
                t_media_ev.set()

        @c_s1.on("participant_media_state")
        async def on_s1_media(d):
            if d.get("user_id") == teacher.id:
                state["s1_teacher_media"] = d
                s1_teacher_media_ev.set()

        @c_t.on("participant_hand_state")
        async def on_t_hand(d):
            state["t_hand"] = d
            t_hand_ev.set()

        @c_t.on("chat_message")
        async def on_t_chat(d):
            state["t_chats"].append(d)
            t_chat_ev.set()

        @c_s1.on("chat_message")
        async def on_s1_chat(d):
            state["s1_chats"].append(d)
            s1_chat_ev.set()

        @c_s2.on("chat_message")
        async def on_s2_chat(d):
            state["s2_chats"].append(d)
            s2_chat_ev.set()

        @c_s1.on("wb_draw")
        async def on_s1_wb(d):
            state["s1_wbs"].append(d)
            s1_wb_ev.set()

        @c_s2.on("wb_draw")
        async def on_s2_wb(d):
            state["s2_wbs"].append(d)
            s2_wb_ev.set()

        @c_s2.on("webrtc_offer")
        async def on_s2_offer(d):
            state["s2_offers"].append(d)
            s2_offer_ev.set()

        @c_s1.on("breakout_entered")
        async def on_s1_be(d):
            s1_bo_entered_ev.set()

        @c_s2.on("breakout_entered")
        async def on_s2_be(d):
            s2_bo_entered_ev.set()

        @c_s1.on("breakout_exited")
        async def on_s1_bx(d):
            s1_bo_exited_ev.set()

        @c_s2.on("breakout_exited")
        async def on_s2_bx(d):
            s2_bo_exited_ev.set()

        @c_s1.on("breakout_announcement")
        async def on_s1_ann(d):
            state["s1_ann"] = d
            s1_announce_ev.set()

        @c_s1.on("recording_state_changed")
        async def on_s1_rec(d):
            state["s1_rec"].append(d)
            s1_rec_ev.set()

        @c_s2.on("participant_removed")
        async def on_s2_rem(d):
            s2_removed_ev.set()

        await c_t.connect(url, socketio_path="/socket.io/", auth={"token": t_tok})
        await c_s1.connect(url, socketio_path="/socket.io/", auth={"token": s1_tok})
        await c_s2.connect(url, socketio_path="/socket.io/", auth={"token": s2_tok})

        await c_t.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await c_s1.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await c_s2.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.gather(
            asyncio.wait_for(t_joined.wait(), timeout=5),
            asyncio.wait_for(s1_joined.wait(), timeout=5),
            asyncio.wait_for(s2_joined.wait(), timeout=5),
            asyncio.wait_for(joined_participants_ev.wait(), timeout=5),
        )
        assert all(not participant["cam_enabled"] for participant in state["joined_participants"])

        # AttendanceSession rows opened automatically for joined users
        assert await AttendanceSession.objects.filter(meeting=meeting, user=s1, left_at__isnull=True).acount() == 1
        assert await AttendanceSession.objects.filter(meeting=meeting, user=s2, left_at__isnull=True).acount() == 1

        # 1. Batch 2: Teacher raises/lowers hand & locks student mic/cam + chat restrictions
        await c_s1.emit("raise_hand", {"meeting_code": meeting.meeting_code, "raised": True})
        await asyncio.wait_for(t_hand_ev.wait(), timeout=5)
        assert state["t_hand"]["hand_raised"] is True
        assert (await MeetingParticipant.objects.aget(meeting=meeting, user=s1)).hand_raised is True

        await c_s1.emit(
            "media_state_change",
            {"meeting_code": meeting.meeting_code, "mic_enabled": True, "cam_enabled": True, "screen_sharing": False},
        )
        await asyncio.wait_for(t_media_ev.wait(), timeout=5)
        assert state["t_media"]["cam_enabled"] is True

        t_media_ev.clear()
        s1_mod_ev.clear()
        await c_t.emit(
            "update_meeting_permissions",
            {"meeting_code": meeting.meeting_code, "allow_student_mic": False, "allow_student_camera": False, "chat_mode": "teacher_only"},
        )
        await asyncio.sleep(0.25)
        assert s1_mod_ev.is_set(), f"Student moderation state was not updated: {state['s1_mod']}"
        s1_sid = next(p["sid"] for p in state["joined_participants"] if p["user_id"] == s1.id)
        assert any(
            event.get("sid") == s1_sid and event.get("cam_enabled") is False
            for event in state["t_media_events"]
        ), f"Teacher did not receive student's camera-off state: {state['t_media_events']}"
        assert state["s1_mod"]["cam_enabled"] is False
        assert state["s1_mod"]["camera_locked"] is True
        assert state["t_media"]["cam_enabled"] is False

        # Student attempts to unmute mic/cam -> rejected & coerced to False
        s1_err_ev.clear()
        t_media_ev.clear()
        await c_s1.emit(
            "media_state_change",
            {"meeting_code": meeting.meeting_code, "mic_enabled": True, "cam_enabled": True, "screen_sharing": False},
        )
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)
        await asyncio.wait_for(t_media_ev.wait(), timeout=5)
        assert state["t_media"]["mic_enabled"] is False
        assert state["t_media"]["cam_enabled"] is False

        # Student camera policy must never disable the meeting teacher's camera.
        await c_t.emit(
            "media_state_change",
            {"meeting_code": meeting.meeting_code, "mic_enabled": True, "cam_enabled": True, "screen_sharing": False},
        )
        await asyncio.wait_for(s1_teacher_media_ev.wait(), timeout=5)
        assert state["s1_teacher_media"]["cam_enabled"] is True
        assert state["s1_teacher_media"]["camera_locked"] is False

        # Student blocked in teacher_only chat mode; teacher succeeds
        s1_err_ev.clear()
        t_chat_ev.clear()
        await c_s1.emit("chat_message", {"meeting_code": meeting.meeting_code, "text": "Blocked chat"})
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)
        await asyncio.sleep(0.15)
        assert len(state["t_chats"]) == 0
        await c_t.emit("chat_message", {"meeting_code": meeting.meeting_code, "text": "Allowed teacher note"})
        await asyncio.wait_for(t_chat_ev.wait(), timeout=5)

        # Re-open chat_mode="everyone" and per-mute s1 chat
        await c_t.emit("update_meeting_permissions", {"meeting_code": meeting.meeting_code, "chat_mode": "everyone"})
        s1_mod_ev.clear()
        t_media_ev.clear()
        await c_t.emit("moderate_participant", {"meeting_code": meeting.meeting_code, "action": "mute_chat", "user_id": s1.id})
        await asyncio.gather(
            asyncio.wait_for(s1_mod_ev.wait(), timeout=5),
            asyncio.wait_for(t_media_ev.wait(), timeout=5),
        )
        assert state["s1_mod"]["chat_muted"] is True
        assert state["t_media"]["chat_muted"] is True

        s1_err_ev.clear()
        await c_s1.emit("chat_message", {"meeting_code": meeting.meeting_code, "text": "Still chat-muted"})
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)

        # Unmute s1 chat & unlock mic/cam for breakout test
        await c_t.emit("moderate_participant", {"meeting_code": meeting.meeting_code, "action": "unmute_chat", "user_id": s1.id})
        await c_t.emit(
            "update_meeting_permissions",
            {"meeting_code": meeting.meeting_code, "allow_student_mic": True, "allow_student_camera": True},
        )
        await asyncio.sleep(0.2)

        # 2. Batch 3: Breakout isolation (Whiteboard, Chat, WebRTC Offer, Return Policy, Announcement, Close)
        await c_t.emit("breakout_action", {"meeting_code": meeting.meeting_code, "action": "create", "names": ["Room A", "Room B"], "duration_minutes": 10})
        await asyncio.sleep(0.25)
        rooms = [r async for r in BreakoutRoom.objects.filter(meeting=meeting).order_by("created_at", "name")]
        assert len(rooms) == 2
        r_a, r_b = rooms[0], rooms[1]

        await c_t.emit("breakout_action", {"meeting_code": meeting.meeting_code, "action": "assign", "breakout_id": str(r_a.id), "user_ids": [s1.id]})
        await c_t.emit("breakout_action", {"meeting_code": meeting.meeting_code, "action": "assign", "breakout_id": str(r_b.id), "user_ids": [s2.id]})
        await c_t.emit("breakout_action", {"meeting_code": meeting.meeting_code, "action": "set_return_policy", "allow_return_to_main": False})
        await asyncio.sleep(0.2)

        await c_t.emit("breakout_action", {"meeting_code": meeting.meeting_code, "action": "open_all", "duration_minutes": 10})
        await asyncio.gather(
            asyncio.wait_for(s1_bo_entered_ev.wait(), timeout=5),
            asyncio.wait_for(s2_bo_entered_ev.wait(), timeout=5),
        )

        # s1 in Room A sends wb_draw & chat -> s2 in Room B MUST NOT receive them
        state["s2_wbs"].clear()
        state["s2_chats"].clear()
        await c_s1.emit("wb_draw", {"meeting_code": meeting.meeting_code, "id": "bo-a-1", "x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5})
        await c_s1.emit("chat_message", {"meeting_code": meeting.meeting_code, "text": "Room A chat only"})
        await asyncio.sleep(0.3)
        assert len(state["s2_wbs"]) == 0
        assert all(m["text"] != "Room A chat only" for m in state["s2_chats"])
        assert await WhiteboardOperation.objects.filter(meeting=meeting, breakout_room=r_a).acount() == 1
        assert await MeetingChatMessage.objects.filter(meeting=meeting, breakout_room=r_a, text="Room A chat only").acount() == 1

        # Cross-subchannel WebRTC signaling blocked (s1 in Room A -> s2 in Room B)
        await c_s1.emit("webrtc_offer", {"meeting_code": meeting.meeting_code, "target_sid": c_s2.sid, "sdp": {"type": "offer", "sdp": "v=0"}})
        await asyncio.sleep(0.2)
        assert len(state["s2_offers"]) == 0

        # s1 cannot return to main while allow_return_to_main=False
        s1_err_ev.clear()
        await c_s1.emit("switch_breakout_room", {"meeting_code": meeting.meeting_code, "breakout_room_id": None})
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)

        # Broadcast announcement reaches Room A
        await c_t.emit("breakout_action", {"meeting_code": meeting.meeting_code, "action": "broadcast_message", "message": "2 minutes left!"})
        await asyncio.wait_for(s1_announce_ev.wait(), timeout=5)
        assert state["s1_ann"]["message"] == "2 minutes left!"

        # Close all breakouts returns both students to Main Room
        await c_t.emit("breakout_action", {"meeting_code": meeting.meeting_code, "action": "close_all"})
        await asyncio.gather(
            asyncio.wait_for(s1_bo_exited_ev.wait(), timeout=5),
            asyncio.wait_for(s2_bo_exited_ev.wait(), timeout=5),
        )

        # 3. Batch 5: Recording state sync (student blocked, teacher toggles & broadcasts)
        s1_err_ev.clear()
        await c_s1.emit("start_recording", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)
        assert (await Meeting.objects.aget(id=meeting.id)).is_recording is False

        s1_rec_ev.clear()
        await c_t.emit("start_recording", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_rec_ev.wait(), timeout=5)
        assert state["s1_rec"][-1]["is_recording"] is True
        assert (await Meeting.objects.aget(id=meeting.id)).is_recording is True

        s1_rec_ev.clear()
        await c_t.emit("stop_recording", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_rec_ev.wait(), timeout=5)
        assert state["s1_rec"][-1]["is_recording"] is False

        # 4. Eviction via moderate_participant(remove) emits participant_removed & closes AttendanceSession
        await c_t.emit("moderate_participant", {"meeting_code": meeting.meeting_code, "action": "remove", "user_id": s2.id})
        await asyncio.wait_for(s2_removed_ev.wait(), timeout=5)
        assert (await MeetingParticipant.objects.aget(meeting=meeting, user=s2)).admission_status == MeetingParticipant.ADMISSION_REMOVED
        assert await AttendanceSession.objects.filter(meeting=meeting, user=s2, left_at__isnull=True).acount() == 0
    finally:
        for client in (c_t, c_s1, c_s2):
            if client.connected:
                await client.disconnect()
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)
