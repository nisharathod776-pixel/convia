import uuid

from django.utils import timezone

from .server import sio
from .validators import room_channel, verify_sid_in_room


@sio.event
async def chat_message(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return

    text = str(data.get("text", "")).strip()
    if not text:
        return

    await sio.emit(
        "chat_message",
        {
            "id": uuid.uuid4().hex,
            "sender_id": user_data["user_id"],
            "sender_username": user_data["username"],
            "sender_name": user_data.get("full_name") or user_data["username"],
            "text": text[:2000],
            "timestamp": timezone.now().isoformat(),
        },
        room=room_channel(code),
    )
