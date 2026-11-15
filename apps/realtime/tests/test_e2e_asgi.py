import asyncio
import socket

import pytest
import socketio
import uvicorn
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

from apps.meetings.models import Meeting, MeetingParticipant

User = get_user_model()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_live_asgi_socketio_webrtc_signaling_and_whiteboard():
    host = await User.objects.acreate(username="e2ehost", email="e2ehost@example.com", full_name="Host User", is_active=True)
    await sync_to_async(host.set_password)("Password123!")
    await sync_to_async(host.save)()

    guest = await User.objects.acreate(username="e2eguest", email="e2eguest@example.com", full_name="Guest User", is_active=True)
    await sync_to_async(guest.set_password)("Password123!")
    await sync_to_async(guest.save)()

    meeting = await Meeting.objects.acreate(title="Live Socket Test", host=host)
    await MeetingParticipant.objects.acreate(meeting=meeting, user=host, role="host", is_active=True)
    await MeetingParticipant.objects.acreate(meeting=meeting, user=guest, role="participant", is_active=True)

    host_refresh = await sync_to_async(RefreshToken.for_user)(host)
    guest_refresh = await sync_to_async(RefreshToken.for_user)(guest)
    host_token = str(host_refresh.access_token)
    guest_token = str(guest_refresh.access_token)

    port = _free_port()
    from avora_project.asgi import application

    config = uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    for _ in range(40):
        if server.started:
            break
        await asyncio.sleep(0.1)
    assert server.started, "Uvicorn ASGI server failed to start"

    url = f"http://127.0.0.1:{port}"
    c_host = socketio.AsyncClient()
    c_guest = socketio.AsyncClient()
    c_bad = socketio.AsyncClient()

    try:
        with pytest.raises(socketio.exceptions.ConnectionError):
            await c_bad.connect(url, socketio_path="/socket.io/", auth={"token": "bad.token.value"})

        host_events = {"room_joined": None, "participant_joined": None, "answer": None}
        guest_events = {"room_joined": None, "offer": None, "wb_draw": None, "wb_clear": None, "ended": None}

        host_joined_ev = asyncio.Event()
        guest_joined_ev = asyncio.Event()
        part_joined_ev = asyncio.Event()
        offer_ev = asyncio.Event()
        answer_ev = asyncio.Event()
        wb_draw_ev = asyncio.Event()
        wb_clear_ev = asyncio.Event()
        ended_ev = asyncio.Event()

        @c_host.on("room_joined")
        async def on_host_joined(data):
            host_events["room_joined"] = data
            host_joined_ev.set()

        @c_host.on("participant_joined")
        async def on_host_part_joined(data):
            host_events["participant_joined"] = data
            part_joined_ev.set()

        @c_host.on("webrtc_answer")
        async def on_host_answer(data):
            host_events["answer"] = data
            answer_ev.set()

        @c_guest.on("room_joined")
        async def on_guest_joined(data):
            guest_events["room_joined"] = data
            guest_joined_ev.set()

        @c_guest.on("webrtc_offer")
        async def on_guest_offer(data):
            guest_events["offer"] = data
            offer_ev.set()

        @c_guest.on("wb_draw")
        async def on_guest_wb(data):
            guest_events["wb_draw"] = data
            wb_draw_ev.set()

        @c_guest.on("wb_clear")
        async def on_guest_wb_clear(data):
            guest_events["wb_clear"] = data
            wb_clear_ev.set()

        @c_guest.on("meeting_ended")
        async def on_guest_ended(data):
            guest_events["ended"] = data
            ended_ev.set()

        await c_host.connect(url, socketio_path="/socket.io/", auth={"token": host_token})
        await c_host.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(host_joined_ev.wait(), timeout=5)

        await c_host.emit(
            "wb_draw",
            {
                "meeting_code": meeting.meeting_code,
                "id": "stroke-1",
                "x1": 0.1,
                "y1": 0.2,
                "x2": 0.4,
                "y2": 0.5,
                "color": "#1D4ED8",
                "size": 4,
            },
        )
        await asyncio.sleep(0.15)

        await c_guest.connect(url, socketio_path="/socket.io/", auth={"token": guest_token})
        await c_guest.emit("join_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(guest_joined_ev.wait(), timeout=5)
        await asyncio.wait_for(part_joined_ev.wait(), timeout=5)

        assert len(guest_events["room_joined"]["peers"]) == 1
        assert len(guest_events["room_joined"]["whiteboard_ops"]) == 1
        assert guest_events["room_joined"]["whiteboard_ops"][0]["id"] == "stroke-1"

        guest_sid = guest_events["room_joined"]["self_sid"]
        host_sid = host_events["room_joined"]["self_sid"]

        await c_host.emit("webrtc_offer", {"meeting_code": meeting.meeting_code, "target_sid": guest_sid, "sdp": {"type": "offer", "sdp": "v=0"}})
        await asyncio.wait_for(offer_ev.wait(), timeout=5)
        assert guest_events["offer"]["from_sid"] == host_sid

        await c_guest.emit("webrtc_answer", {"meeting_code": meeting.meeting_code, "target_sid": host_sid, "sdp": {"type": "answer", "sdp": "v=0"}})
        await asyncio.wait_for(answer_ev.wait(), timeout=5)
        assert host_events["answer"]["sdp"]["type"] == "answer"

        await c_host.emit(
            "wb_draw",
            {
                "meeting_code": meeting.meeting_code,
                "id": "stroke-2",
                "x1": 0.2,
                "y1": 0.3,
                "x2": 0.6,
                "y2": 0.7,
                "color": "#0F172A",
                "size": 6,
            },
        )
        await asyncio.wait_for(wb_draw_ev.wait(), timeout=5)
        assert guest_events["wb_draw"]["id"] == "stroke-2"

        await c_host.emit("wb_clear", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(wb_clear_ev.wait(), timeout=5)

        await c_host.emit("end_meeting", {"meeting_code": meeting.meeting_code})
        await asyncio.wait_for(ended_ev.wait(), timeout=5)
        assert guest_events["ended"]["meeting_code"] == meeting.meeting_code
    finally:
        if c_host.connected:
            await c_host.disconnect()
        if c_guest.connected:
            await c_guest.disconnect()
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)
