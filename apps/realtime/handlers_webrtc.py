from .server import sio
from .validators import ROOM_PEERS, room_channel, verify_sid_in_room


def _target_in_same_room(code: str, target_sid: str) -> bool:
    return bool(target_sid and code in ROOM_PEERS and target_sid in ROOM_PEERS[code])


@sio.event
async def webrtc_offer(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    target_sid = (data or {}).get("target_sid")
    sdp = (data or {}).get("sdp")
    if not _target_in_same_room(code, target_sid) or not sdp:
        return
    sender_info = ROOM_PEERS[code].get(sid)
    await sio.emit(
        "webrtc_offer",
        {"from_sid": sid, "sdp": sdp, "participant": sender_info},
        to=target_sid,
    )


@sio.event
async def webrtc_answer(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    target_sid = (data or {}).get("target_sid")
    sdp = (data or {}).get("sdp")
    if not _target_in_same_room(code, target_sid) or not sdp:
        return
    await sio.emit("webrtc_answer", {"from_sid": sid, "sdp": sdp}, to=target_sid)


@sio.event
async def webrtc_ice_candidate(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    target_sid = (data or {}).get("target_sid")
    candidate = (data or {}).get("candidate")
    if not _target_in_same_room(code, target_sid) or not candidate:
        return
    await sio.emit("webrtc_ice_candidate", {"from_sid": sid, "candidate": candidate}, to=target_sid)


@sio.event
async def media_state_change(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    peer = ROOM_PEERS[code].get(sid)
    if not peer:
        return
    mic = bool((data or {}).get("mic_enabled", True))
    cam = bool((data or {}).get("cam_enabled", True))
    scr = bool((data or {}).get("screen_sharing", False))

    peer["mic_enabled"] = mic
    peer["cam_enabled"] = cam
    peer["screen_sharing"] = scr

    await sio.emit(
        "participant_media_state",
        {"sid": sid, "mic_enabled": mic, "cam_enabled": cam, "screen_sharing": scr},
        room=room_channel(code),
        skip_sid=sid,
    )
