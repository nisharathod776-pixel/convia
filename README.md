# CONVIA — Real-Time Communication & Collaboration Platform

CONVIA is a full-stack real-time video conferencing and collaboration web application built with **HTML5, CSS3, Vanilla ES6+ JavaScript, Python, Django, Django REST Framework, Python Socket.IO (ASGI), WebRTC, Django File Storage, and HTML5 Canvas**.

---

## Architecture & Technology Stack

- **Frontend:** Pure HTML5 templates, CSS3 design system (`frontend/static/css/`), Vanilla ES6+ JavaScript modules (`frontend/static/js/`), HTML5 Canvas 2D API, and vendored Socket.IO v4 client (`frontend/static/vendor/socket.io.min.js`).
- **Backend:** Python 3.11, Django 5.1, Django REST Framework, `djangorestframework-simplejwt` (JWT access & rotated/blacklisted refresh tokens).
- **Real-Time Transport:** `python-socketio` (`AsyncServer`) mounted alongside Django's ASGI application (`convia_project/asgi.py`) and served by Uvicorn.
- **Video / Audio / Screen Share:** Browser WebRTC (`RTCPeerConnection`, `getUserMedia`, `getDisplayMedia`) with server-provided STUN/TURN configuration.
- **Meeting Collaboration:** Room-scoped live chat and a synchronized whiteboard with colored drawing, erasing, and shape tools. Whiteboard operations are stored for late joiners; chat messages are live only.
- **Database:** SQLite (with WAL mode enabled for concurrent ASGI access) out of the box, PostgreSQL-ready via `DATABASE_URL` (`dj-database-url` + `psycopg`).
- **File Sharing:** Django `FileSystemStorage` under `media/meeting_files/` with extension, MIME type, and file-size validation, served only through authenticated participant-verified download endpoints.

---

## Local Setup & Running CONVIA

1. **Create & activate a Python virtual environment:**
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set a strong `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and optional `TURN_URL`, `TURN_USERNAME`, and `TURN_CREDENTIAL` for symmetric-NAT WebRTC traversal.

3. **Apply database migrations & collect static files:**
   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   ```

4. **Create an administrator account (optional, for `/admin-panel/` and `/django-admin/`):**
   ```bash
   python manage.py createsuperuser
   ```

5. **Run the unified Django + Socket.IO ASGI server:**
   ```bash
   uvicorn convia_project.asgi:application --host 0.0.0.0 --port 3000
   ```
   Open `http://localhost:3000` in your browser.

   Do not use `python manage.py runserver` for meetings: Django's development server runs the WSGI application and does not serve the Socket.IO ASGI endpoint.

---

## PostgreSQL & Production Deployment

- Set `DATABASE_URL="postgres://USER:PASSWORD@HOST:5432/DBNAME"` in `.env`.
- Set `DJANGO_DEBUG="False"`, a cryptographic `DJANGO_SECRET_KEY`, exact `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, and `CORS_ALLOWED_ORIGINS` using `https://`.
- Terminate TLS (HTTPS/WSS) in front of Uvicorn (e.g., Nginx / Cloud Run / Caddy), as browsers require HTTPS for `getUserMedia` and `getDisplayMedia`.
- Configure a STUN/TURN server (`coturn` or managed TURN) via `TURN_URL`, `TURN_USERNAME`, and `TURN_CREDENTIAL`.

---

## Automated Testing

Run the full Django & ASGI Socket.IO test suite:
```bash
.venv/bin/pytest -v
```

Run focused whiteboard drawing logic checks:
```bash
node --test frontend/tests/meeting-whiteboard.test.js
```

Test coverage includes:
- `apps/accounts/tests/test_accounts.py`: Registration, password hashing, email/username login, JWT access/refresh rotation & blacklisting on logout, profile updates, and deactivated user rejection.
- `apps/meetings/tests/test_meetings.py`: Meeting creation, unique code generation, join/leave, participant-gated ICE configuration, host-only meeting termination, and ended-meeting rejection.
- `apps/fileshare/tests/test_fileshare.py`: Disallowed extension rejection, validated upload, participant-only listing/downloads, and non-member `403` enforcement.
- `apps/whiteboard/tests/test_whiteboard.py`: Whiteboard state recovery for late joiners, board clearing, and authorization checks.
- `apps/admin_panel/tests/test_admin_panel.py`: Server-side admin RBAC (`403` for non-admins), user activation/deactivation, self-deactivation guard, and overview metrics.
- `apps/realtime/tests/test_realtime.py` & `test_e2e_asgi.py`: Live Uvicorn ASGI + Socket.IO JWT handshake rejection/acceptance, bidirectional chat and whiteboard room isolation, WebRTC offer/answer relay, persisted whiteboard drawing/text, reconnect state recovery, and meeting-ended broadcast.
- `frontend/tests/meeting-whiteboard.test.js`: Drag-coordinate previews and commits for every shape, colour retention, normalized resizing, and eraser behavior.
