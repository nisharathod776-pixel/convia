import logging
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
import socketio
from rest_framework_simplejwt.authentication import JWTAuthentication

logger = logging.getLogger("apps.realtime")

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=settings.CORS_ALLOWED_ORIGINS,
    ping_interval=25,
    ping_timeout=20,
)


@sync_to_async
def _authenticate_jwt(raw_token: str):
    if not raw_token:
        return None
    jwt_auth = JWTAuthentication()
    validated = jwt_auth.get_validated_token(raw_token)
    user = jwt_auth.get_user(validated)
    if not user or not user.is_active:
        return None
    return {
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name or user.username,
        "is_staff": user.is_staff,
        "role": user.role,
    }


@sio.event
async def connect(sid, environ, auth):
    token = None
    if isinstance(auth, dict):
        token = auth.get("token")
    if not token:
        auth_header = environ.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    try:
        user_data = await _authenticate_jwt(token)
    except Exception as exc:
        logger.warning("Socket.IO auth rejected for sid=%s: %s", sid, exc)
        raise socketio.exceptions.ConnectionRefusedError("Authentication failed")

    if not user_data:
        raise socketio.exceptions.ConnectionRefusedError("Authentication required")

    await sio.save_session(sid, {"user": user_data, "rooms": set()})
    logger.info("Socket connected sid=%s user=%s", sid, user_data["username"])
