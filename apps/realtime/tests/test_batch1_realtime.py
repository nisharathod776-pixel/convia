import asyncio
import socket

import pytest
import socketio
import uvicorn
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

from apps.meetings.models import Meeting, MeetingChatMessage, MeetingParticipant
from apps.whiteboard.models import WhiteboardOperation

User = get_user_model()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _create_user_and_token(username, full_name, role="member"):
    user = await User.objects.acreate(
        username=username,
        email=f"{username}@example.com",
        full_name=full_name,
        role=role,
        is_active=True,
    )
    await sync_to_async(user.set_password)("Password123!")
    await sync_to_async(user.save)()
    refresh = await sync_to_async(RefreshToken.for_user)(user)
    return user, str(refresh.access_token)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_batch1_waiting_room_admission_whiteboard_chat_and_screenshare_realtime():
    teacher, t_tok = await _create_user_and_token("b1_teacher", "Prof. Ada")
    s1, s1_tok = await _create_user_and_token("b1_stud1", "Student One")
    s2, s2_tok = await _create_user_and_token("b1_stud2", "Student Two")

    meeting = await Meeting.objects.acreate(
        title="Batch 1 Realtime Classroom",
        host=teacher,
        waiting_room_enabled=True,
        whiteboard_student_write_enabled=False,
        allow_student_screenshare=False,
    )
    await MeetingParticipant.objects.acreate(
        meeting=meeting, user=teacher, role="teacher", admission_status="admitted", is_active=True
    )
    await MeetingParticipant.objects.acreate(
        meeting=meeting, user=s1, role="student", admission_status="waiting", is_active=False
    )
    await MeetingParticipant.objects.acreate(
        meeting=meeting, user=s2, role="student", admission_status="waiting", is_active=False
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
        t_req_ev = asyncio.Event()
        t_chat_ev = asyncio.Event()
        t_chat_del_ev = asyncio.Event()
        t_wb_draw_ev = asyncio.Event()
        s1_wait_ev = asyncio.Event()
        s1_err_ev = asyncio.Event()
        s1_admit_ev = asyncio.Event()
        s1_joined_ev = asyncio.Event()
        s1_perm_ev = asyncio.Event()
        s1_share_grant_ev = asyncio.Event()
        s1_share_deny_ev = asyncio.Event()
        s1_share_revoke_ev = asyncio.Event()
        s2_wait_ev = asyncio.Event()
        s2_reject_ev = asyncio.Event()
        s2_joined_ev = asyncio.Event()
        s2_share_grant_ev = asyncio.Event()
        s2_share_deny_ev = asyncio.Event()
        s1_spoof_media_ev = asyncio.Event()

        state = {
            "t_requests": [],
            "t_chats": [],
            "t_deleted": [],
            "s1_errors": [],
            "s1_room": None,
            "s1_share": None,
            "s2_share": None,
            "s1_spoof_media": None,
        }

        @c_t.on("room_joined")
        async def on_t_joined(d):
            t_joined.set()

        @c_t.on("admission_requested")
        async def on_t_req(d):
            state["t_requests"].append(d)
            t_req_ev.set()

        @c_t.on("chat_message")
        async def on_t_chat(d):
            state["t_chats"].append(d)
            t_chat_ev.set()

        @c_t.on("chat_message_deleted")
        async def on_t_del(d):
            state["t_deleted"].append(d)
            t_chat_del_ev.set()

        @c_t.on("wb_draw")
        async def on_t_wb(d):
            t_wb_draw_ev.set()

        @c_s1.on("waiting_room_joined")
        async def on_s1_wait(d):
            s1_wait_ev.set()

        @c_s1.on("error_event")
        async def on_s1_err(d):
            state["s1_errors"].append(d)
            s1_err_ev.set()

        @c_s1.on("admission_approved")
        async def on_s1_admit(d):
            s1_admit_ev.set()

        @c_s1.on("room_joined")
        async def on_s1_joined(d):
            state["s1_room"] = d
            s1_joined_ev.set()

        @c_s1.on("meeting_permissions_updated")
        async def on_s1_perm(d):
            s1_perm_ev.set()

        @c_s1.on("screen_share_granted")
        async def on_s1_sg(d):
            state["s1_share"] = "granted"
            s1_share_grant_ev.set()

        @c_s1.on("screen_share_denied")
        async def on_s1_sd(d):
            state["s1_share"] = "denied"
            s1_share_deny_ev.set()

        @c_s1.on("screen_share_revoked")
        async def on_s1_sr(d):
            s1_share_revoke_ev.set()

        @c_s2.on("waiting_room_joined")
        async def on_s2_wait(d):
            s2_wait_ev.set()

        @c_s2.on("admission_rejected")
        async def on_s2_rej(d):
            s2_reject_ev.set()

        @c_s2.on("room_joined")
        async def on_s2_joined(d):
            s2_joined_ev.set()

        @c_s2.on("screen_share_granted")
        async def on_s2_sg(d):
            state["s2_share"] = "granted"
            s2_share_grant_ev.set()

        @c_s2.on("screen_share_denied")
        async def on_s2_sd(d):
            state["s2_share"] = "denied"
            s2_share_deny_ev.set()

        @c_t.on("participant_media_state")
        async def on_t_media(d):
            state["s1_spoof_media"] = d
            s1_spoof_media_ev.set()

        await c_t.connect(url, socketio_path="/socket.io/", auth={"token": t_tok})
        await c_t.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(t_joined.wait(), timeout=5)

        await c_s1.connect(url, socketio_path="/socket.io/", auth={"token": s1_tok})
        await c_s2.connect(url, socketio_path="/socket.io/", auth={"token": s2_tok})

        # Unadmitted student cannot bypass waiting room via direct join_meeting
        await c_s1.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)
        assert not s1_joined_ev.is_set()
        s1_err_ev.clear()

        # Student 1 enters waiting room -> teacher receives notification
        await c_s1.emit("join_waiting_room", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_wait_ev.wait(), timeout=5)
        await asyncio.wait_for(t_req_ev.wait(), timeout=5)
        assert state["t_requests"][0]["message"] == "Student Student One is requesting to join your meeting."

        # Student 2 enters waiting room, then teacher rejects Student 2
        t_req_ev.clear()
        await c_s2.emit("join_waiting_room", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s2_wait_ev.wait(), timeout=5)
        await asyncio.wait_for(t_req_ev.wait(), timeout=5)
        await c_t.emit("admission_decision", {"meeting_code": meeting.meeting_code, "user_id": s2.id, "decision": "reject"})
        await asyncio.wait_for(s2_reject_ev.wait(), timeout=5)
        assert not s2_joined_ev.is_set()

        # Teacher admits Student 1 -> Student 1 joins room
        await c_t.emit("admission_decision", {"meeting_code": meeting.meeting_code, "user_id": s1.id, "decision": "admit"})
        await asyncio.wait_for(s1_admit_ev.wait(), timeout=5)
        await c_s1.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_joined_ev.wait(), timeout=5)
        assert state["s1_room"]["meeting_role"] == "student"

        # Student 1 cannot draw or clear whiteboard by default
        await c_s1.emit(
            "wb_draw",
            {"meeting_code": meeting.meeting_code, "id": "bad-draw", "x1": 0.1, "y1": 0.1, "x2": 0.3, "y2": 0.3},
        )
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)
        assert await WhiteboardOperation.objects.filter(meeting=meeting).acount() == 0
        s1_err_ev.clear()

        # Teacher enables student drawing -> Student 1 can draw, still cannot clear
        await c_t.emit(
            "update_meeting_permissions",
            {"meeting_code": meeting.meeting_code, "whiteboard_student_write_enabled": True},
        )
        await asyncio.wait_for(s1_perm_ev.wait(), timeout=5)
        await c_s1.emit(
            "wb_draw",
            {"meeting_code": meeting.meeting_code, "id": "stud-draw", "x1": 0.1, "y1": 0.1, "x2": 0.3, "y2": 0.3},
        )
        await asyncio.wait_for(t_wb_draw_ev.wait(), timeout=5)
        assert await WhiteboardOperation.objects.filter(meeting=meeting).acount() == 1

        await c_s1.emit("wb_clear", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)
        assert await WhiteboardOperation.objects.filter(meeting=meeting).acount() == 1
        s1_err_ev.clear()

        # Chat message deletion RBAC over Socket.IO
        await c_t.emit("chat_message", {"meeting_code": meeting.meeting_code, "text": "Teacher link https://convia.edu"})
        await asyncio.wait_for(t_chat_ev.wait(), timeout=5)
        teacher_msg_id = state["t_chats"][-1]["id"]
        t_chat_ev.clear()

        await c_s1.emit("chat_message", {"meeting_code": meeting.meeting_code, "text": "Student 1 comment"})
        await asyncio.wait_for(t_chat_ev.wait(), timeout=5)
        s1_msg_id = state["t_chats"][-1]["id"]
        t_chat_ev.clear()

        # Student cannot delete teacher message
        await c_s1.emit("delete_chat_message", {"meeting_code": meeting.meeting_code, "message_id": teacher_msg_id})
        await asyncio.wait_for(s1_err_ev.wait(), timeout=5)
        assert (await MeetingChatMessage.objects.aget(id=teacher_msg_id)).is_deleted is False
        s1_err_ev.clear()

        # Student can delete their own message
        await c_s1.emit("delete_chat_message", {"meeting_code": meeting.meeting_code, "message_id": s1_msg_id})
        await asyncio.wait_for(t_chat_del_ev.wait(), timeout=5)
        assert state["t_deleted"][-1]["message_id"] == s1_msg_id
        t_chat_del_ev.clear()

        # Teacher can moderate another student's message
        s2_msg = await MeetingChatMessage.objects.acreate(
            meeting=meeting, sender=s2, sender_role="student", text="Inappropriate note"
        )
        await c_t.emit("delete_chat_message", {"meeting_code": meeting.meeting_code, "message_id": str(s2_msg.id)})
        await asyncio.wait_for(t_chat_del_ev.wait(), timeout=5)
        assert (await MeetingChatMessage.objects.aget(id=s2_msg.id)).is_deleted is True

        # Screen share RBAC & race condition protection
        await c_s1.emit("request_screen_share", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s1_share_deny_ev.wait(), timeout=5)
        s1_share_deny_ev.clear()
        state["s1_share"] = None

        # Admit Student 2 directly in DB to test concurrent student race
        await MeetingParticipant.objects.filter(meeting=meeting, user=s2).aupdate(
            admission_status="admitted", is_active=True
        )
        await c_s2.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(s2_joined_ev.wait(), timeout=5)

        s1_perm_ev.clear()
        await c_t.emit(
            "update_meeting_permissions",
            {"meeting_code": meeting.meeting_code, "allow_student_screenshare": True},
        )
        await asyncio.wait_for(s1_perm_ev.wait(), timeout=5)

        # Simultaneous screen share requests -> only 1 wins
        await asyncio.gather(
            c_s1.emit("request_screen_share", {"meeting_code": meeting.meeting_code}),
            c_s2.emit("request_screen_share", {"meeting_code": meeting.meeting_code}),
        )
        await asyncio.sleep(0.4)
        outcomes = sorted([str(state["s1_share"]), str(state["s2_share"])])
        assert outcomes == ["denied", "granted"]

        # Ensure winner is s1 (or whoever won) and test teacher preemption
        meeting_db = await Meeting.objects.aget(id=meeting.id)
        assert meeting_db.active_screen_sharer_id in (s1.id, s2.id)

        # Teacher preempts active student sharer
        await c_t.emit("request_screen_share", {"meeting_code": meeting.meeting_code})
        await asyncio.sleep(0.3)
        meeting_db = await Meeting.objects.aget(id=meeting.id)
        assert meeting_db.active_screen_sharer_id == teacher.id

        # Non-sharer spoofing media_state_change(screen_sharing=True) is neutralized
        s1_spoof_media_ev.clear()
        await c_s1.emit(
            "media_state_change",
            {"meeting_code": meeting.meeting_code, "mic_enabled": True, "cam_enabled": True, "screen_sharing": True},
        )
        await asyncio.wait_for(s1_spoof_media_ev.wait(), timeout=5)
        assert state["s1_spoof_media"]["screen_sharing"] is False
    finally:
        for client in (c_t, c_s1, c_s2):
            if client.connected:
                await client.disconnect()
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)
