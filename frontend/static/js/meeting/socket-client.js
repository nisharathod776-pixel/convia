import { getAccessToken, refreshAccessToken } from '../core/api.js';

export class MeetingSocketClient {
  constructor(meetingCode, handlers = {}) {
    this.meetingCode = meetingCode;
    this.handlers = handlers;
    this.socket = null;
    this.hasJoined = false;
    this.waitingMode = false;
    this.hasJoinedWaiting = false;
    this.authRefreshAttempted = false;
  }

  connect({ waitingRoom = false } = {}) {
    if (typeof window.io !== 'function') {
      throw new Error('Socket.IO client library is not loaded.');
    }

    this.waitingMode = Boolean(waitingRoom);
    if (this.socket) {
      if (!this.waitingMode && this.socket.connected && !this.hasJoined) {
        this.socket.emit('join_meeting', { meeting_code: this.meetingCode });
      }
      return;
    }

    const token = getAccessToken();
    this.socket = window.io({
      path: '/socket.io/',
      transports: ['polling', 'websocket'],
      auth: { token },
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
    });

    this.socket.on('connect', () => {
      this.authRefreshAttempted = false;
      this.handlers.onConnectionState?.('connected');
      if (this.waitingMode) {
        this.socket.emit('join_waiting_room', { meeting_code: this.meetingCode });
      } else {
        this.socket.emit('join_meeting', { meeting_code: this.meetingCode });
      }
    });

    this.socket.on('disconnect', (reason) => {
      this.hasJoined = false;
      this.hasJoinedWaiting = false;
      this.handlers.onConnectionState?.('disconnected', reason);
      if (reason === 'io server disconnect' && this.socket) {
        this.socket.connect();
      }
    });

    this.socket.io.on('reconnect_attempt', () => {
      this.handlers.onConnectionState?.('reconnecting');
    });

    this.socket.on('connect_error', (err) => {
      const socket = this.socket;
      this.handlers.onConnectionState?.('reconnecting');
      if (!socket || socket.active || this.authRefreshAttempted) {
        if (socket && !socket.active) {
          this.handlers.onConnectionState?.('disconnected');
          this.handlers.onError?.(err?.message || 'Real-time connection failed.');
        }
        return;
      }

      this.authRefreshAttempted = true;
      refreshAccessToken().then((freshToken) => {
        if (this.socket !== socket) return;
        if (!freshToken) {
          this.handlers.onConnectionState?.('disconnected');
          this.handlers.onError?.(err?.message || 'Real-time connection failed.');
          return;
        }
        socket.auth = { token: freshToken };
        socket.connect();
      });
    });

    this.socket.on('error_event', (payload) => {
      this.handlers.onError?.(payload?.message || 'Room error.');
    });

    this.socket.on('waiting_room_joined', (payload) => {
      this.hasJoinedWaiting = true;
      this.handlers.onWaitingRoomJoined?.(payload);
    });
    this.socket.on('waiting_room_updated', (payload) => this.handlers.onWaitingRoomUpdated?.(payload));
    this.socket.on('admission_requested', (payload) => this.handlers.onAdmissionRequested?.(payload));
    this.socket.on('admission_approved', (payload) => this.handlers.onAdmissionApproved?.(payload));
    this.socket.on('admission_rejected', (payload) => this.handlers.onAdmissionRejected?.(payload));

    this.socket.on('room_joined', (payload) => {
      this.waitingMode = false;
      this.hasJoinedWaiting = false;
      this.hasJoined = true;
      this.handlers.onRoomJoined?.(payload);
    });

    this.socket.on('participant_joined', (payload) => this.handlers.onParticipantJoined?.(payload));
    this.socket.on('participant_left', (payload) => this.handlers.onParticipantLeft?.(payload));
    this.socket.on('participant_media_state', (payload) => this.handlers.onMediaState?.(payload));
    this.socket.on('participant_hand_state', (payload) => this.handlers.onParticipantHandState?.(payload));
    this.socket.on('moderation_enforced', (payload) => this.handlers.onModerationEnforced?.(payload));
    this.socket.on('participant_removed', (payload) => this.handlers.onParticipantRemoved?.(payload));
    this.socket.on('meeting_permissions_updated', (payload) => this.handlers.onPermissionsUpdated?.(payload));
    this.socket.on('meeting_ended', (payload) => this.handlers.onMeetingEnded?.(payload));

    this.socket.on('breakout_state_updated', (payload) => this.handlers.onBreakoutStateUpdated?.(payload));
    this.socket.on('breakout_entered', (payload) => this.handlers.onBreakoutEntered?.(payload));
    this.socket.on('breakout_exited', (payload) => this.handlers.onBreakoutExited?.(payload));
    this.socket.on('breakout_announcement', (payload) => this.handlers.onBreakoutAnnouncement?.(payload));

    this.socket.on('recording_state_changed', (payload) => this.handlers.onRecordingStateChanged?.(payload));
    this.socket.on('attendance_updated', (payload) => this.handlers.onAttendanceUpdated?.(payload));

    this.socket.on('webrtc_offer', (payload) => this.handlers.onWebRtcOffer?.(payload));
    this.socket.on('webrtc_answer', (payload) => this.handlers.onWebRtcAnswer?.(payload));
    this.socket.on('webrtc_ice_candidate', (payload) => this.handlers.onWebRtcIce?.(payload));

    this.socket.on('screen_share_granted', (payload) => this.handlers.onScreenShareGranted?.(payload));
    this.socket.on('screen_share_denied', (payload) => this.handlers.onScreenShareDenied?.(payload));
    this.socket.on('screen_share_revoked', (payload) => this.handlers.onScreenShareRevoked?.(payload));
    this.socket.on('screen_share_state', (payload) => this.handlers.onScreenShareState?.(payload));

    this.socket.on('whiteboard_state', (payload) => this.handlers.onWhiteboardState?.(payload));
    this.socket.on('wb_draw', (payload) => this.handlers.onWhiteboardDraw?.(payload));
    this.socket.on('wb_text', (payload) => this.handlers.onWhiteboardText?.(payload));
    this.socket.on('wb_clear', (payload) => this.handlers.onWhiteboardClear?.(payload));
    this.socket.on('chat_message', (payload) => this.handlers.onChatMessage?.(payload));
    this.socket.on('chat_message_deleted', (payload) => this.handlers.onChatMessageDeleted?.(payload));

    this.socket.on('file_shared', (payload) => this.handlers.onFileShared?.(payload));
  }

  upgradeToRoom() {
    this.waitingMode = false;
    if (this.socket && this.socket.connected) {
      this.socket.emit('join_meeting', { meeting_code: this.meetingCode });
    } else if (!this.socket) {
      this.connect({ waitingRoom: false });
    }
  }

  emit(event, data = {}) {
    if (!this.socket || !this.socket.connected || !this.hasJoined) return false;
    this.socket.emit(event, { meeting_code: this.meetingCode, ...data });
    return true;
  }

  disconnect() {
    if (this.socket) {
      if (this.hasJoinedWaiting && !this.hasJoined && this.socket.connected) {
        this.socket.emit('leave_waiting_room', { meeting_code: this.meetingCode });
      }
      this.emit('leave_meeting', {});
      this.socket.disconnect();
      this.socket = null;
    }
    this.hasJoined = false;
    this.hasJoinedWaiting = false;
  }
}
