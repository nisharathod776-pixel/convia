import os
import django
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "convia_project.settings")
django.setup()

import socketio  # noqa: E402
from apps.realtime.server import sio  # noqa: E402
import apps.realtime.handlers_chat  # noqa: F401,E402
import apps.realtime.handlers_meeting  # noqa: F401,E402
import apps.realtime.handlers_webrtc  # noqa: F401,E402
import apps.realtime.handlers_whiteboard  # noqa: F401,E402
from apps.realtime.handlers_meeting import (  # noqa: E402
    start_breakout_expiry_scheduler,
    stop_breakout_expiry_scheduler,
)

django_asgi_app = get_asgi_application()
application = socketio.ASGIApp(
    sio,
    other_asgi_app=django_asgi_app,
    socketio_path="socket.io",
    on_startup=start_breakout_expiry_scheduler,
    on_shutdown=stop_breakout_expiry_scheduler,
)
