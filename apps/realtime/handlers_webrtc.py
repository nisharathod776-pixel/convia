from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.meetings.models import ActivityLog, Meeting, MeetingParticipant
from apps.meetings.permissions import can_use_camera, can_use_mic, is_meeting_teacher
from .server import sio
from .validators import (
    ROOM_PEERS,
    control_channel,
    get_peer_subchannel,
    get_room_lock,
    same_subchannel,
    verify_sid_in_room,
)

User = get_user_model()


@sync_to_async
def _acquire_screen_share_lock(meeting_code: str, user_id: int, sid: str, active_sids: set[str]):
    with transaction.atomic():
        user = User.objects.filter(id=user_id, is_active=True).first()
        meeting = (
            Meeting.objects.select_for_update()
            .select_related("host", "active_screen_sharer")
            .filter(meeting_code=meeting_code.lower())
            .first()
        )
        if not user or not meeting or meeting.status == Meeting.STATUS_ENDED:
            return False, "Meeting is unavailable.", None, None

        is_teacher = is_meeting_teacher(user, meeting)
        current_sid = meeting.active_screen_sharer_sid or ""
        has_live_sharer = bool(meeting.active_screen_sharer_id and current_sid in active_sids)

        if has_live_sharer:
            if current_sid == sid:
                return True, None, None, {
                    "meeting_code": meeting.meeting_code,
                    "active_screen_sharer_id": user.id,
                    "active_screen_sharer_username": user.username,
                    "active_screen_sharer_sid": sid,
                }
            if is_teacher:
                revoked_sid = current_sid
            else:
                return False, "Another participant is already sharing their screen.", None, None
        else:
            revoked_sid = None
            if not is_teacher and not meeting.allow_student_screenshare:
                ActivityLog.objects.create(
                    user=user,
                    action="unauthorized_screenshare_attempt",
                    detail=meeting.meeting_code,
                )
                return False, "Screen sharing is restricted by the teacher.", None, None

        meeting.active_screen_sharer = user
        meeting.active_screen_sharer_sid = sid
        meeting.save(update_fields=["active_screen_sharer", "active_screen_sharer_sid"])
        return True, None, revoked_sid, {
            "meeting_code": meeting.meeting_code,
            "active_screen_sharer_id": user.id,
            "active_screen_sharer_username": user.username,
            "active_screen_sharer_sid": sid,
        }


@sync_to_async
def _release_screen_share_lock(meeting_code: str, user_id: int, sid: str):
    with transaction.atomic():
        user = User.objects.filter(id=user_id, is_active=True).first()
        meeting = (
            Meeting.objects.select_for_update()
            .select_related("host")
            .filter(meeting_code=meeting_code.lower())
            .first()
        )
        if not user or not meeting:
            return False, None, None
        is_teacher = is_meeting_teacher(user, meeting)
        prev_sid = meeting.active_screen_sharer_sid or ""
        if not meeting.active_screen_sharer_id:
            return True, None, {
                "meeting_code": meeting.meeting_code,
                "active_screen_sharer_id": None,
                "active_screen_sharer_username": None,
                "active_screen_sharer_sid": "",
            }
        if prev_sid != sid and meeting.active_screen_sharer_id != user.id and not is_teacher:
            return False, None, None

        revoked_sid = prev_sid if (is_teacher and prev_sid and prev_sid != sid) else None
        meeting.active_screen_sharer = None
        meeting.active_screen_sharer_sid = ""
        meeting.save(update_fields=["active_screen_sharer", "active_screen_sharer_sid"])
        return True, revoked_sid, {
            "meeting_code": meeting.meeting_code,
            "active_screen_sharer_id": None,
            "active_screen_sharer_username": None,
            "active_screen_sharer_sid": "",
        }


@sync_to_async
def _get_media_permissions(meeting_code: str, user_id: int, sid: str):
    user = User.objects.filter(id=user_id, is_active=True).first()
    meeting = Meeting.objects.select_related("host").filter(meeting_code=meeting_code.lower()).first()
    if not user or not meeting:
        return False, False, False
    participant = MeetingParticipant.objects.filter(meeting=meeting, user=user).first()
    allow_mic = can_use_mic(user, meeting, participant)
    allow_cam = can_use_camera(user, meeting, participant)
    scr_allowed = meeting.active_screen_sharer_sid == sid
    return allow_mic, allow_cam, scr_allowed


@sync_to_async
def _set_hand_raised(meeting_code: str, user_id: int, desired=None):
    with transaction.atomic():
        participant = (
            MeetingParticipant.objects.select_for_update()
            .filter(
                meeting__meeting_code=meeting_code.lower(),
                user_id=user_id,
                admission_status=MeetingParticipant.ADMISSION_ADMITTED,
            )
            .first()
        )
        if not participant:
            return False, False
        new_state = (not participant.hand_raised) if desired is None else bool(desired)
        if participant.hand_raised != new_state:
            participant.hand_raised = new_state
            participant.save(update_fields=["hand_raised"])
        return True, new_state


@sio.event
async def webrtc_offer(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    target_sid = (data or {}).get("target_sid")
    sdp = (data or {}).get("sdp")
    if not same_subchannel(code, sid, target_sid) or not sdp:
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
    if not same_subchannel(code, sid, target_sid) or not sdp:
        return
    await sio.emit("webrtc_answer", {"from_sid": sid, "sdp": sdp}, to=target_sid)


@sio.event
async def webrtc_ice_candidate(sid, data):
    code, _ = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code:
        return
    target_sid = (data or {}).get("target_sid")
    candidate = (data or {}).get("candidate")
    if not same_subchannel(code, sid, target_sid) or not candidate:
        return
    await sio.emit("webrtc_ice_candidate", {"from_sid": sid, "candidate": candidate}, to=target_sid)


@sio.event
async def request_screen_share(sid, data):
    code, user_data = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        return

    async with get_room_lock(code):
        active_sids = set(ROOM_PEERS.get(code, {}).keys())
        ok, err, revoked_sid, state = await _acquire_screen_share_lock(
            code, user_data["user_id"], sid, active_sids
        )
        if not ok:
            await sio.emit("screen_share_denied", {"meeting_code": code, "message": err}, to=sid)
            return

        sender_channel = get_peer_subchannel(code, sid)
        if revoked_sid and revoked_sid in ROOM_PEERS.get(code, {}):
            revoked_peer = ROOM_PEERS[code][revoked_sid]
            revoked_peer["screen_sharing"] = False
            revoked_channel = get_peer_subchannel(code, revoked_sid)
            await sio.emit(
                "screen_share_revoked",
                {"meeting_code": code, "message": "The teacher started screen sharing."},
                to=revoked_sid,
            )
            await sio.emit(
                "participant_media_state",
                {
                    "sid": revoked_sid,
                    "mic_enabled": revoked_peer.get("mic_enabled", True),
                    "cam_enabled": revoked_peer.get("cam_enabled", True),
                    "screen_sharing": False,
                },
                room=revoked_channel,
            )
            if revoked_channel != sender_channel:
                await sio.emit("screen_share_state", state, room=revoked_channel)

        peer = ROOM_PEERS.get(code, {}).get(sid)
        if peer:
            peer["screen_sharing"] = True
            await sio.emit(
                "participant_media_state",
                {
                    "sid": sid,
                    "mic_enabled": peer.get("mic_enabled", True),
                    "cam_enabled": peer.get("cam_enabled", True),
                    "screen_sharing": True,
                },
                room=sender_channel,
                skip_sid=sid,
            )

        await sio.emit("screen_share_granted", state, to=sid)
        await sio.emit("screen_share_state", state, room=sender_channel)


@sio.event
async def stop_screen_share(sid, data):
    code, user_data = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        return

    async with get_room_lock(code):
        ok, revoked_sid, state = await _release_screen_share_lock(code, user_data["user_id"], sid)
        if not ok:
            await sio.emit(
                "error_event",
                {"message": "You do not have permission to stop this screen share."},
                to=sid,
            )
            return

        target_sid = revoked_sid or sid
        target_channel = get_peer_subchannel(code, target_sid)
        peer = ROOM_PEERS.get(code, {}).get(target_sid)
        if peer:
            peer["screen_sharing"] = False
            await sio.emit(
                "participant_media_state",
                {
                    "sid": target_sid,
                    "mic_enabled": peer.get("mic_enabled", True),
                    "cam_enabled": peer.get("cam_enabled", True),
                    "screen_sharing": False,
                },
                room=target_channel,
            )
        if revoked_sid:
            await sio.emit(
                "screen_share_revoked",
                {"meeting_code": code, "message": "Your screen share was stopped by the teacher."},
                to=revoked_sid,
            )
        await sio.emit("screen_share_state", state, room=target_channel)
        sender_channel = get_peer_subchannel(code, sid)
        if sender_channel != target_channel:
            await sio.emit("screen_share_state", state, room=sender_channel)


@sio.event
async def media_state_change(sid, data):
    code, user_data = await verify_sid_in_room(sid, (data or {}).get("meeting_code"))
    if not code or not user_data:
        return
    peer = ROOM_PEERS[code].get(sid)
    if not peer:
        return

    allow_mic, allow_cam, scr_allowed = await _get_media_permissions(code, user_data["user_id"], sid)

    requested_mic = bool((data or {}).get("mic_enabled", True))
    requested_cam = bool((data or {}).get("cam_enabled", False))
    requested_scr = bool((data or {}).get("screen_sharing", False))

    mic = requested_mic if allow_mic else False
    cam = requested_cam if allow_cam else False
    scr = requested_scr and scr_allowed

    peer["mic_enabled"] = mic
    peer["cam_enabled"] = cam
    peer["mic_locked"] = not allow_mic
    peer["camera_locked"] = not allow_cam
    peer["screen_sharing"] = scr

    if (requested_mic and not allow_mic) or (requested_cam and not allow_cam):
        await sio.emit(
            "error_event",
            {"message": "Audio or camera control is locked by the teacher."},
            to=sid,
        )
        await sio.emit(
            "moderation_enforced",
            {
                "meeting_code": code,
                "mic_enabled": mic,
                "cam_enabled": cam,
                "mic_locked": not allow_mic,
                "camera_locked": not allow_cam,
            },
            to=sid,
        )

    await sio.emit(
        "participant_media_state",
        {
            "sid": sid,
            "user_id": user_data["user_id"],
            "mic_enabled": mic,
            "cam_enabled": cam,
            "screen_sharing": scr,
            "mic_locked": not allow_mic,
            "camera_locked": not allow_cam,
            "chat_muted": peer.get("chat_muted", False),
        },
        room=get_peer_subchannel(code, sid),
        skip_sid=sid,
    )


@sio.event
async def raise_hand(sid, data):
    data = data if isinstance(data, dict) else {}
    code, user_data = await verify_sid_in_room(sid, data.get("meeting_code"))
    if not code or not user_data:
        return
    desired = data.get("raised") if "raised" in data else None
    ok, new_state = await _set_hand_raised(code, user_data["user_id"], desired=desired)
    if not ok:
        return
    peer = ROOM_PEERS.get(code, {}).get(sid)
    if peer:
        peer["hand_raised"] = new_state
    await sio.emit(
        "participant_hand_state",
        {
            "meeting_code": code,
            "sid": sid,
            "user_id": user_data["user_id"],
            "username": user_data["username"],
            "hand_raised": new_state,
        },
        room=control_channel(code),
    )
