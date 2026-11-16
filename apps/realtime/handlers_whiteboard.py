import math
import re
import uuid

from asgiref.sync import sync_to_async
from apps.meetings.models import Meeting
from apps.whiteboard.models import WhiteboardOperation
from .server import sio
from .validators import room_channel, verify_sid_in_room


def _clamp01(value, default=0.0):
    try:
        num = float(value)
        if not math.isfinite(num):
            return default
        return max(0.0, min(1.0, num))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_size(value, default=4, minimum=1, maximum=60):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_color(value):
    color = str(value or "")
    return color if re.fullmatch(r"#[0-9A-Fa-f]{6}", color) else "#0F172A"


def _operation_id(value):
    return str(value or uuid.uuid4().hex)[:64]


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
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return
    tool = str(data.get("tool", "pen"))
    allowed_tools = {"pen", "highlighter", "eraser", "line", "rectangle", "ellipse", "triangle", "arrow"}
    if tool not in allowed_tools:
        return
    payload = {
        "id": _operation_id(data.get("id")),
        "type": "draw",
        "tool": tool,
        "x1": _clamp01(data.get("x1")),
        "y1": _clamp01(data.get("y1")),
        "x2": _clamp01(data.get("x2")),
        "y2": _clamp01(data.get("y2")),
        "color": _safe_color(data.get("color", "#0F172A")),
        "size": _safe_size(data.get("size", 4), maximum=80),
    }
    await _persist_wb_op(code, user_data["user_id"], payload["id"], WhiteboardOperation.TYPE_DRAW, payload)
    await sio.emit("wb_draw", payload, room=room_channel(code), skip_sid=sid)


@sio.event
async def wb_text(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return
    text = str(data.get("text", "")).strip()[:300]
    if not text:
        return
    payload = {
        "id": _operation_id(data.get("id")),
        "type": "text",
        "x": _clamp01(data.get("x")),
        "y": _clamp01(data.get("y")),
        "text": text,
        "color": _safe_color(data.get("color", "#0F172A")),
        "size": _safe_size(data.get("size", 18), default=18, minimum=10, maximum=64),
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
