import math
import re
import uuid

from asgiref.sync import sync_to_async
from apps.meetings.models import ActivityLog, Meeting
from apps.whiteboard.models import WhiteboardOperation
from .server import sio
from .validators import (
    check_whiteboard_permission,
    get_peer_breakout_id,
    room_subchannel,
    verify_sid_in_room,
)


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
def _persist_wb_op(meeting_code: str, user_id: int, op_id: str, op_type: str, payload: dict, breakout_room_id=None):
    meeting = Meeting.objects.filter(meeting_code=meeting_code).first()
    if not meeting:
        return
    WhiteboardOperation.objects.create(
        meeting=meeting,
        user_id=user_id,
        breakout_room_id=breakout_room_id or None,
        op_id=op_id[:64],
        op_type=op_type,
        payload=payload,
    )


@sync_to_async
def _clear_wb_ops(meeting_code: str, breakout_room_id=None):
    qs = WhiteboardOperation.objects.filter(meeting__meeting_code=meeting_code)
    if breakout_room_id:
        qs = qs.filter(breakout_room_id=breakout_room_id)
    else:
        qs = qs.filter(breakout_room__isnull=True)
    qs.delete()


@sync_to_async
def _log_unauthorized_wb(user_id: int, meeting_code: str, action: str):
    ActivityLog.objects.create(user_id=user_id, action=action, detail=meeting_code)


@sio.event
async def wb_draw(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return
    breakout_room_id = get_peer_breakout_id(code, sid)
    if not await check_whiteboard_permission(
        user_data["user_id"], code, require_clear=False, breakout_room_id=breakout_room_id
    ):
        await _log_unauthorized_wb(user_data["user_id"], code, "unauthorized_wb_draw")
        await sio.emit(
            "error_event",
            {"message": "Only the teacher can draw or edit the whiteboard."},
            to=sid,
        )
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
        "breakout_room_id": str(breakout_room_id) if breakout_room_id else None,
    }
    await _persist_wb_op(
        code, user_data["user_id"], payload["id"], WhiteboardOperation.TYPE_DRAW, payload, breakout_room_id
    )
    await sio.emit("wb_draw", payload, room=room_subchannel(code, breakout_room_id), skip_sid=sid)


@sio.event
async def wb_text(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return
    breakout_room_id = get_peer_breakout_id(code, sid)
    if not await check_whiteboard_permission(
        user_data["user_id"], code, require_clear=False, breakout_room_id=breakout_room_id
    ):
        await _log_unauthorized_wb(user_data["user_id"], code, "unauthorized_wb_text")
        await sio.emit(
            "error_event",
            {"message": "Only the teacher can add text to the whiteboard."},
            to=sid,
        )
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
        "breakout_room_id": str(breakout_room_id) if breakout_room_id else None,
    }
    await _persist_wb_op(
        code, user_data["user_id"], payload["id"], WhiteboardOperation.TYPE_TEXT, payload, breakout_room_id
    )
    await sio.emit("wb_text", payload, room=room_subchannel(code, breakout_room_id), skip_sid=sid)


@sio.event
async def wb_clear(sid, data):
    code, user_data = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        return
    breakout_room_id = get_peer_breakout_id(code, sid)
    if not await check_whiteboard_permission(
        user_data["user_id"], code, require_clear=True, breakout_room_id=breakout_room_id
    ):
        await _log_unauthorized_wb(user_data["user_id"], code, "unauthorized_wb_clear")
        await sio.emit(
            "error_event",
            {"message": "Only the teacher can clear the whiteboard."},
            to=sid,
        )
        return
    await _clear_wb_ops(code, breakout_room_id)
    await sio.emit(
        "wb_clear",
        {"meeting_code": code, "breakout_room_id": str(breakout_room_id) if breakout_room_id else None},
        room=room_subchannel(code, breakout_room_id),
        skip_sid=sid,
    )
