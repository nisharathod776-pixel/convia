from asgiref.sync import sync_to_async
from apps.meetings.models import Meeting
from apps.whiteboard.models import WhiteboardOperation
from .server import sio
from .validators import room_channel, verify_sid_in_room


def _clamp01(value, default=0.0):
    try:
        num = float(value)
        return max(0.0, min(1.0, num))
    except (TypeError, ValueError):
        return default


@sync_to_async
def _persist_wb_op(meeting_code: str, user_id: int, op_id: str, op_type: str, payload: dict):
    meeting = Meeting.objects.filter(meeting_code=meeting_code).first()
    if not meeting:
        return
    WhiteboardOperation.objects.create(
        meeting=meeting,
        user_id=user_id,
        op_id=op_id[:64],
        op_type=op_type,
        payload=payload,
    )


@sync_to_async
def _clear_wb_ops(meeting_code: str):
    WhiteboardOperation.objects.filter(meeting__meeting_code=meeting_code).delete()


@sio.event
async def wb_draw(sid, data):
    code, user_data = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        return
    payload = {
        "id": str((data or {}).get("id", ""))[:64],
        "type": "draw",
        "x1": _clamp01((data or {}).get("x1")),
        "y1": _clamp01((data or {}).get("y1")),
        "x2": _clamp01((data or {}).get("x2")),
        "y2": _clamp01((data or {}).get("y2")),
        "color": str((data or {}).get("color", "#0F172A"))[:16],
        "size": max(1, min(60, int((data or {}).get("size", 4)))),
    }
    await _persist_wb_op(code, user_data["user_id"], payload["id"], WhiteboardOperation.TYPE_DRAW, payload)
    await sio.emit("wb_draw", payload, room=room_channel(code), skip_sid=sid)


@sio.event
async def wb_text(sid, data):
    code, user_data = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        return
    text = str((data or {}).get("text", "")).strip()[:300]
    if not text:
        return
    payload = {
        "id": str((data or {}).get("id", ""))[:64],
        "type": "text",
        "x": _clamp01((data or {}).get("x")),
        "y": _clamp01((data or {}).get("y")),
        "text": text,
        "color": str((data or {}).get("color", "#0F172A"))[:16],
        "size": max(10, min(64, int((data or {}).get("size", 18)))),
    }
    await _persist_wb_op(code, user_data["user_id"], payload["id"], WhiteboardOperation.TYPE_TEXT, payload)
    await sio.emit("wb_text", payload, room=room_channel(code), skip_sid=sid)


@sio.event
async def wb_clear(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    await _clear_wb_ops(code)
    await sio.emit("wb_clear", {"meeting_code": code}, room=room_channel(code), skip_sid=sid)
