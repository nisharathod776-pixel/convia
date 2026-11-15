import { getAccessToken, refreshAccessToken } from '../core/api.js';

export class MeetingSocketClient {
  constructor(meetingCode, handlers = {}) {
    this.meetingCode = meetingCode;
    this.handlers = handlers;
    this.socket = null;
    this.hasJoined = false;
  }

  connect() {
    if (typeof window.io !== 'function') {
      throw new Error('Socket.IO client library is not loaded.');
    }

    const token = getAccessToken();
    this.socket = window.io({
      path: '/socket.io/',
      transports: ['websocket', 'polling'],
      auth: { token },
      reconnection: true,
      reconnectionAttempts: 10,
      reconnectionDelay: 1000,
    });

    this.socket.on('connect', () => {
      this.handlers.onConnectionState?.('connected');
      this.socket.emit('join_meeting', { meeting_code: this.meetingCode });
    });

    this.socket.on('disconnect', (reason) => {
      this.hasJoined = false;
      this.handlers.onConnectionState?.('disconnected', reason);
    });

    this.socket.io.on('reconnect_attempt', async () => {
      this.handlers.onConnectionState?.('reconnecting');
      const freshToken = await refreshAccessToken();
      if (freshToken && this.socket) {
        this.socket.auth = { token: freshToken };
      }
    });

    this.socket.on('connect_error', (err) => {
      this.handlers.onError?.(err?.message || 'Real-time connection failed.');
    });

    this.socket.on('error_event', (payload) => {
      this.handlers.onError?.(payload?.message || 'Room error.');
    });

    this.socket.on('room_joined', (payload) => {
      this.hasJoined = true;
      this.handlers.onRoomJoined?.(payload);
    });

    this.socket.on('participant_joined', (payload) => this.handlers.onParticipantJoined?.(payload));
    this.socket.on('participant_left', (payload) => this.handlers.onParticipantLeft?.(payload));
    this.socket.on('participant_media_state', (payload) => this.handlers.onMediaState?.(payload));
    this.socket.on('meeting_ended', (payload) => this.handlers.onMeetingEnded?.(payload));

    this.socket.on('webrtc_offer', (payload) => this.handlers.onWebRtcOffer?.(payload));
    this.socket.on('webrtc_answer', (payload) => this.handlers.onWebRtcAnswer?.(payload));
    this.socket.on('webrtc_ice_candidate', (payload) => this.handlers.onWebRtcIce?.(payload));

    this.socket.on('whiteboard_state', (payload) => this.handlers.onWhiteboardState?.(payload));
    this.socket.on('wb_draw', (payload) => this.handlers.onWhiteboardDraw?.(payload));
    this.socket.on('wb_text', (payload) => this.handlers.onWhiteboardText?.(payload));
    this.socket.on('wb_clear', (payload) => this.handlers.onWhiteboardClear?.(payload));

    this.socket.on('file_shared', (payload) => this.handlers.onFileShared?.(payload));
  }

  emit(event, data = {}) {
    if (this.socket && this.socket.connected) {
      this.socket.emit(event, { meeting_code: this.meetingCode, ...data });
    }
  }

  disconnect() {
    if (this.socket) {
      this.emit('leave_meeting', {});
      this.socket.disconnect();
      this.socket = null;
    }
  }
}
