import { getInitials, showToast } from '../core/ui.js';

const CAMERA_CONSTRAINTS = {
  facingMode: { ideal: 'user' },
  width: { ideal: 1280 },
  height: { ideal: 720 },
};

function getMediaErrorMessage(error, device) {
  if (['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(error?.name)) {
    return `${device} access was denied. Allow access in your browser settings and try again.`;
  }
  if (['NotFoundError', 'DevicesNotFoundError'].includes(error?.name)) {
    return `No ${device.toLowerCase()} was found. Connect a ${device.toLowerCase()} and try again.`;
  }
  if (['NotReadableError', 'TrackStartError'].includes(error?.name)) {
    return `${device} is unavailable or being used by another application. Close that application and try again.`;
  }
  if (['OverconstrainedError', 'ConstraintNotSatisfiedError'].includes(error?.name)) {
    return `The ${device.toLowerCase()} does not support the requested settings.`;
  }
  if (error?.name === 'AbortError') {
    return `${device} startup was interrupted. Try again.`;
  }
  return error?.message
    ? `${device} could not be started: ${error.message}`
    : `Unable to start ${device.toLowerCase()}. Check browser permissions and device availability.`;
}

export class PeerConnectionManager {
  constructor({
    socketClient,
    iceServers,
    localUser,
    onParticipantsChanged,
    onTrackAdded,
    onTrackRemoved,
    onPeerRemoved,
    refreshCameraPermissions,
  }) {
    this.socketClient = socketClient;
    this.iceServers = iceServers || [{ urls: 'stun:stun.l.google.com:19302' }];
    this.localUser = localUser;
    this.onParticipantsChanged = onParticipantsChanged;
    this.onTrackAdded = onTrackAdded;
    this.onTrackRemoved = onTrackRemoved;
    this.onPeerRemoved = onPeerRemoved;
    this.refreshCameraPermissions = refreshCameraPermissions;

    this.localSid = null;
    this.localStream = null;
    this.cameraTrack = null;
    this.screenStream = null;

    this.micEnabled = true;
    this.camEnabled = true;
    this.isScreenSharing = false;
    this.isTeacher = false;
    this.allowStudentScreenshare = false;
    this.allowStudentMic = true;
    this.allowStudentCamera = true;
    this.micLocked = false;
    this.cameraLocked = false;
    this.handRaised = false;
    this.activeScreenSharerId = null;
    this.activeScreenSharerUsername = null;
    this.activeScreenSharerSid = '';
    this._pendingShareRequest = null;
    this._cameraToggleInProgress = false;
    this._observedCameraTracks = new WeakSet();
    this.isCleaningUp = false;

    this.peers = new Map(); // sid -> { pc, participant, pendingIce, stream, videoEl, tileEl }
  }

  async initLocalMedia() {
    const localVideo = document.getElementById('local-video');
    const localName = document.getElementById('local-name-label');
    const fallbackName = document.getElementById('local-fallback-name');
    const initialsEl = document.getElementById('local-avatar-initials');

    const displayName = `${this.localUser.full_name || this.localUser.username} (You)`;
    if (localName) localName.textContent = displayName;
    if (fallbackName) fallbackName.textContent = displayName;
    if (initialsEl) initialsEl.textContent = getInitials(this.localUser.full_name || this.localUser.username);

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      this.micEnabled = false;
      this.camEnabled = false;
      this.updateLocalMediaUi();
      showToast('Camera/microphone API is unavailable in this browser context.', 'warning');
      return;
    }

    const audioConstraints = { echoCancellation: true, noiseSuppression: true };
    if (this.isTeacher) {
      try {
        this.localStream = await navigator.mediaDevices.getUserMedia({
          video: CAMERA_CONSTRAINTS,
          audio: audioConstraints,
        });
        this.cameraTrack = this.getLiveCameraTrack();
        this.camEnabled = Boolean(this.cameraTrack);
        if (!this.cameraTrack) {
          showToast('No usable camera track was returned. You joined with microphone only.', 'warning');
        }
      } catch (cameraAndAudioError) {
        try {
          this.localStream = await navigator.mediaDevices.getUserMedia({
            audio: audioConstraints,
            video: false,
          });
          this.cameraTrack = null;
          this.camEnabled = false;
          showToast(
            `Camera did not start; you joined with microphone only. ${getMediaErrorMessage(cameraAndAudioError, 'Camera')}`,
            'warning',
          );
        } catch (audioError) {
          this.localStream = new MediaStream();
          this.cameraTrack = null;
          this.camEnabled = false;
          this.micEnabled = false;
          showToast(
            `Camera and microphone could not be started. ${getMediaErrorMessage(audioError, 'Microphone')}`,
            'warning',
          );
        }
      }
    } else {
      try {
        this.localStream = await navigator.mediaDevices.getUserMedia({
          audio: audioConstraints,
          video: false,
        });
        this.cameraTrack = null;
        this.camEnabled = false;
      } catch (audioError) {
        this.localStream = new MediaStream();
        this.cameraTrack = null;
        this.camEnabled = false;
        this.micEnabled = false;
        showToast(getMediaErrorMessage(audioError, 'Microphone'), 'warning');
      }
    }

    this.micEnabled = Boolean(this.localStream.getAudioTracks().length);
    if (this.cameraTrack) {
      this.cameraTrack.enabled = this.camEnabled;
      this.observeCameraTrack(this.cameraTrack);
    }
    if (localVideo) localVideo.srcObject = this.localStream;

    this.updateLocalMediaUi();
  }

  getLiveCameraTrack() {
    const videoTracks = this.localStream?.getVideoTracks() || [];
    const track = [this.cameraTrack, ...videoTracks].find(
      (candidate) => candidate?.readyState === 'live',
    ) || null;
    videoTracks.forEach((videoTrack) => {
      if (videoTrack.readyState !== 'live' && this.localStream?.getTracks().includes(videoTrack)) {
        this.localStream.removeTrack(videoTrack);
      }
    });
    this.cameraTrack = track;
    return track;
  }

  handleCameraTrackEnded(track) {
    if (this.isCleaningUp || this.cameraTrack !== track) return;
    if (this.localStream?.getTracks().includes(track)) this.localStream.removeTrack(track);
    this.cameraTrack = null;
    this.camEnabled = false;
    this.replaceVideoTrackForPeers(null).catch((error) => {
      console.error('Unable to remove the ended camera track from peers:', error);
    });
    this.updateLocalMediaUi();
    this.broadcastMediaState();
  }

  observeCameraTrack(track) {
    if (this._observedCameraTracks.has(track)) return;
    this._observedCameraTracks.add(track);
    if (typeof track.addEventListener === 'function') {
      track.addEventListener('ended', () => this.handleCameraTrackEnded(track), { once: true });
    } else {
      track.onended = () => this.handleCameraTrackEnded(track);
    }
  }

  setLocalSid(sid) {
    this.localSid = sid;
  }

  async syncRoomPeers(peersList = []) {
    if (!Array.isArray(peersList)) peersList = [];
    const activeSids = new Set(
      peersList
        .filter((peerInfo) => peerInfo?.sid && peerInfo.sid !== this.localSid)
        .map((peerInfo) => peerInfo.sid),
    );
    for (const sid of this.peers.keys()) {
      if (!activeSids.has(sid)) this.removePeer(sid);
    }

    for (const peerInfo of peersList) {
      if (!peerInfo?.sid) continue;
      if (peerInfo.sid === this.localSid) continue;
      const shouldOffer = this.localSid && this.localSid > peerInfo.sid;
      await this.ensurePeer(peerInfo, shouldOffer);
    }
    this.notifyParticipants();
  }

  async handleParticipantJoined(peerInfo) {
    if (!peerInfo || peerInfo.sid === this.localSid) return;
    const shouldOffer = this.localSid && this.localSid > peerInfo.sid;
    await this.ensurePeer(peerInfo, shouldOffer);
    this.notifyParticipants();
  }

  async ensurePeer(peerInfo, initiateOffer = false) {
    const sid = peerInfo.sid;
    if (this.peers.has(sid)) {
      const existing = this.peers.get(sid);
      if (existing.pc.connectionState !== 'closed' && existing.pc.connectionState !== 'failed') {
        existing.participant = { ...existing.participant, ...peerInfo };
        this.updateRemoteTileUi(sid);
        return existing;
      }
      this.removePeer(sid);
    }

    const pc = new RTCPeerConnection({ iceServers: this.iceServers });
    const remoteStream = new MediaStream();
    const peerEntry = {
      pc,
      participant: peerInfo,
      pendingIce: [],
      stream: remoteStream,
      audioTrackIds: new Set(),
      tileEl: null,
      videoEl: null,
    };
    this.peers.set(sid, peerEntry);
    this.createRemoteTile(sid, peerEntry);

    if (this.localStream) {
      const activeVideoTrack = this.isScreenSharing && this.screenStream
        ? this.screenStream.getVideoTracks()[0]
        : this.cameraTrack;
      const audioTrack = this.localStream.getAudioTracks()[0];
      if (audioTrack) pc.addTrack(audioTrack, this.localStream);
      if (activeVideoTrack) pc.addTrack(activeVideoTrack, this.localStream);
    }

    pc.onicecandidate = (event) => {
      if (event.candidate) {
        this.socketClient.emit('webrtc_ice_candidate', {
          target_sid: sid,
          candidate: event.candidate,
        });
      }
    };

    pc.ontrack = (event) => {
      const incomingTrack = event?.track;
      if (!incomingTrack) return;

      const alreadyAdded = peerEntry.stream?.getTracks().some((track) => track.id === incomingTrack.id);
      if (peerEntry.stream && !alreadyAdded) {
        peerEntry.stream.addTrack(incomingTrack);
      }
      if (incomingTrack.kind === 'audio') peerEntry.audioTrackIds.add(incomingTrack.id);

      if (peerEntry.videoEl) {
        peerEntry.videoEl.srcObject = peerEntry.stream;
      }

      this.updateRemoteTileUi(sid);

      if (typeof this.onTrackAdded === 'function') {
        this.onTrackAdded(sid, peerEntry, incomingTrack);
      }
      incomingTrack.addEventListener('ended', () => {
        if (peerEntry.stream?.getTracks().some((track) => track.id === incomingTrack.id)) {
          peerEntry.stream.removeTrack(incomingTrack);
        }
        peerEntry.audioTrackIds?.delete(incomingTrack.id);
        this.updateRemoteTileUi(sid);
        this.onTrackRemoved?.(sid, peerEntry, incomingTrack);
      }, { once: true });
    };

    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed') {
        showToast(`Reconnecting media with ${peerInfo.full_name || peerInfo.username}...`, 'warning');
        if (this.localSid && this.localSid > sid) {
          this.sendOffer(sid, { iceRestart: true });
        }
      }
    };

    if (initiateOffer) {
      await this.sendOffer(sid);
    }

    return peerEntry;
  }

  async sendOffer(sid, options = {}) {
    const entry = this.peers.get(sid);
    if (!entry) return;
    try {
      const offer = await entry.pc.createOffer({
        offerToReceiveAudio: true,
        offerToReceiveVideo: true,
        ...options,
      });
      await entry.pc.setLocalDescription(offer);
      this.socketClient.emit('webrtc_offer', {
        target_sid: sid,
        sdp: entry.pc.localDescription,
      });
    } catch (err) {
      console.error('WebRTC offer error:', err);
    }
  }

  async handleOffer({ from_sid, sdp, participant }) {
    if (!from_sid || !sdp) return;
    const peerInfo = participant || { sid: from_sid, username: 'Participant' };
    const entry = await this.ensurePeer(peerInfo, false);

    try {
      await entry.pc.setRemoteDescription(new RTCSessionDescription(sdp));
      await this.flushPendingIce(entry);
      const answer = await entry.pc.createAnswer();
      await entry.pc.setLocalDescription(answer);
      this.socketClient.emit('webrtc_answer', {
        target_sid: from_sid,
        sdp: entry.pc.localDescription,
      });
    } catch (err) {
      console.error('WebRTC answer error:', err);
    }
  }

  async handleAnswer({ from_sid, sdp }) {
    const entry = this.peers.get(from_sid);
    if (!entry || !sdp) return;
    try {
      await entry.pc.setRemoteDescription(new RTCSessionDescription(sdp));
      await this.flushPendingIce(entry);
    } catch (err) {
      console.error('WebRTC setRemoteDescription answer error:', err);
    }
  }

  async handleIceCandidate({ from_sid, candidate }) {
    const entry = this.peers.get(from_sid);
    if (!entry || !candidate) return;
    try {
      if (entry.pc.remoteDescription) {
        await entry.pc.addIceCandidate(new RTCIceCandidate(candidate));
      } else {
        entry.pendingIce.push(candidate);
      }
    } catch (err) {
      console.error('WebRTC ICE candidate error:', err);
    }
  }

  async flushPendingIce(entry) {
    while (entry.pendingIce.length) {
      const c = entry.pendingIce.shift();
      await entry.pc.addIceCandidate(new RTCIceCandidate(c));
    }
  }

  isMicLockedForMe() {
    return !this.isTeacher && (!this.allowStudentMic || this.micLocked);
  }

  isCamLockedForMe() {
    return !this.isTeacher && (!this.allowStudentCamera || this.cameraLocked);
  }

  handleRemoteMediaState({ sid, mic_enabled, cam_enabled, screen_sharing, mic_locked, camera_locked, chat_muted }) {
    const entry = this.peers.get(sid);
    if (!entry) return;
    entry.participant.mic_enabled = mic_enabled;
    entry.participant.cam_enabled = cam_enabled;
    entry.participant.screen_sharing = screen_sharing;
    if (typeof mic_locked === 'boolean') entry.participant.mic_locked = mic_locked;
    if (typeof camera_locked === 'boolean') entry.participant.camera_locked = camera_locked;
    if (typeof chat_muted === 'boolean') entry.participant.chat_muted = chat_muted;
    this.updateRemoteTileUi(sid);
    this.notifyParticipants();
  }

  updateHandState({ sid, user_id, hand_raised }) {
    const val = Boolean(hand_raised);
    if ((sid && sid === this.localSid) || (user_id && Number(user_id) === Number(this.localUser.id))) {
      this.handRaised = val;
      this.updateLocalMediaUi();
    }
    for (const [peerSid, entry] of this.peers.entries()) {
      if (peerSid === sid || (user_id && Number(entry.participant.user_id) === Number(user_id))) {
        entry.participant.hand_raised = val;
        this.updateRemoteTileUi(peerSid);
      }
    }
    this.notifyParticipants();
  }

  applyModeration({ mic_locked, camera_locked, mic_enabled, cam_enabled, hand_raised } = {}) {
    if (typeof mic_locked === 'boolean') this.micLocked = mic_locked;
    if (typeof camera_locked === 'boolean') this.cameraLocked = camera_locked;
    if (typeof hand_raised === 'boolean') this.handRaised = hand_raised;

    const audioTracks = this.localStream?.getAudioTracks() || [];
    if (this.isMicLockedForMe() || mic_enabled === false) {
      this.micEnabled = false;
      audioTracks.forEach((t) => {
        t.enabled = false;
      });
    }
    if ((this.isCamLockedForMe() || cam_enabled === false) && this.cameraTrack) {
      this.camEnabled = false;
      this.cameraTrack.enabled = false;
    }

    this.updateLocalMediaUi();
    this.notifyParticipants();
  }

  removePeer(sid) {
    const entry = this.peers.get(sid);
    if (!entry) return;

    if (typeof this.onPeerRemoved === 'function') {
      this.onPeerRemoved(entry);
    }

    try {
      entry.pc.close();
    } catch {
      // Ignore close error
    }

    if (entry.tileEl) {
      entry.tileEl.remove();
    }

    this.peers.delete(sid);
    this.notifyParticipants();
  }

  resetRemotePeers() {
    for (const sid of Array.from(this.peers.keys())) {
      const entry = this.peers.get(sid);
      if (entry) {
        this.onPeerRemoved?.(entry);
        try {
          entry.pc.close();
        } catch {
          // Ignore close error
        }
        if (entry.tileEl) {
          entry.tileEl.remove();
        }
      }
      this.peers.delete(sid);
    }
    this.notifyParticipants();
  }

  getAudioTracksForRecording() {
    const tracks = [];
    if (this.localStream) {
      this.localStream.getAudioTracks().forEach((t) => tracks.push(t));
    }
    for (const entry of this.peers.values()) {
      if (entry.stream) {
        entry.stream.getAudioTracks().forEach((t) => tracks.push(t));
      }
    }
    return tracks;
  }

  toggleMic() {
    if (this.isMicLockedForMe()) {
      showToast('Your microphone is muted/locked by the teacher.', 'warning');
      return;
    }
    if (!this.localStream) return;
    const tracks = this.localStream.getAudioTracks();
    if (!tracks.length) {
      showToast('No microphone track available.', 'warning');
      return;
    }
    this.micEnabled = !this.micEnabled;
    tracks.forEach((t) => {
      t.enabled = this.micEnabled;
    });
    this.updateLocalMediaUi();
    this.broadcastMediaState();
  }

  async toggleCamera() {
    if (this.isScreenSharing) {
      showToast('Stop screen sharing before toggling camera.', 'warning');
      return;
    }
    if (this.camEnabled) {
      this.camEnabled = false;
      if (this.cameraTrack?.readyState === 'live') this.cameraTrack.enabled = false;
      this.updateLocalMediaUi();
      this.broadcastMediaState();
      return;
    }
    if (this._cameraToggleInProgress) return;

    this._cameraToggleInProgress = true;
    try {
      if (!this.isTeacher && typeof this.refreshCameraPermissions === 'function') {
        let latestPermissions;
        try {
          latestPermissions = await this.refreshCameraPermissions();
        } catch (error) {
          showToast(
            `Unable to verify the current classroom camera permission. ${error.message || 'Try again.'}`,
            'error',
          );
          return;
        }
        if (latestPermissions) this.setPermissions(latestPermissions);
      }
      if (this.isCamLockedForMe()) {
        showToast('The teacher has disabled student cameras. You cannot turn on your camera.', 'warning');
        return;
      }
      if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
        showToast('Camera access is not available in this browser context.', 'error');
        return;
      }

      let track = this.getLiveCameraTrack();
      if (!track) {
        const cameraStream = await navigator.mediaDevices.getUserMedia({
          video: CAMERA_CONSTRAINTS,
          audio: false,
        });
        track = cameraStream.getVideoTracks().find((candidate) => candidate.readyState === 'live') || null;
        if (!track) {
          cameraStream.getTracks().forEach((candidate) => candidate.stop());
          const error = new Error('The browser did not provide a usable camera track.');
          error.name = 'NotFoundError';
          throw error;
        }
        if (this.isCamLockedForMe()) {
          cameraStream.getTracks().forEach((candidate) => candidate.stop());
          showToast('The teacher disabled student cameras before your camera could start.', 'warning');
          return;
        }
        this.cameraTrack = track;
      }

      if (!this.localStream) this.localStream = new MediaStream();
      if (!this.localStream.getTracks().some((candidate) => candidate.id === track.id)) {
        this.localStream.addTrack(track);
      }
      track.enabled = false;
      this.observeCameraTrack(track);
      const localVideo = document.getElementById('local-video');
      if (localVideo) localVideo.srcObject = this.localStream;

      await this.replaceVideoTrackForPeers(track);
      if (this.isCamLockedForMe()) {
        track.enabled = false;
        this.camEnabled = false;
        this.updateLocalMediaUi();
        showToast('The teacher disabled student cameras before your camera could start.', 'warning');
        return;
      }
      track.enabled = true;
      this.camEnabled = true;
      this.updateLocalMediaUi();
      this.broadcastMediaState();
    } catch (error) {
      this.camEnabled = false;
      if (this.cameraTrack?.readyState === 'live') this.cameraTrack.enabled = false;
      this.updateLocalMediaUi();
      showToast(getMediaErrorMessage(error, 'Camera'), 'error');
    } finally {
      this._cameraToggleInProgress = false;
    }
  }

  setPermissions({
    isTeacher = false,
    allowStudentScreenshare = false,
    allowStudentMic = true,
    allowStudentCamera = true,
    cameraLocked,
    activeScreenSharer = null,
  } = {}) {
    this.isTeacher = Boolean(isTeacher);
    this.allowStudentScreenshare = Boolean(allowStudentScreenshare);
    this.allowStudentMic = allowStudentMic !== false;
    this.allowStudentCamera = allowStudentCamera !== false;
    if (typeof cameraLocked === 'boolean') this.cameraLocked = cameraLocked;
    if (activeScreenSharer) {
      this.activeScreenSharerId = activeScreenSharer.id ?? null;
      this.activeScreenSharerUsername = activeScreenSharer.username ?? null;
      this.activeScreenSharerSid = activeScreenSharer.sid || '';
    }
    if (this.isMicLockedForMe() && this.micEnabled) {
      this.micEnabled = false;
      (this.localStream?.getAudioTracks() || []).forEach((t) => {
        t.enabled = false;
      });
    }
    if (this.isCamLockedForMe() && this.camEnabled) {
      this.camEnabled = false;
      if (this.cameraTrack) this.cameraTrack.enabled = false;
    }
    this.updateLocalMediaUi();
    this.notifyParticipants();
  }

  handleScreenShareGranted(payload) {
    this.handleScreenShareState(payload);
    if (this._pendingShareRequest) {
      const { resolve, timer } = this._pendingShareRequest;
      clearTimeout(timer);
      this._pendingShareRequest = null;
      resolve(payload);
    }
  }

  handleScreenShareDenied(payload) {
    const message = payload?.message || 'Screen sharing is not permitted right now.';
    if (this._pendingShareRequest) {
      const { reject, timer } = this._pendingShareRequest;
      clearTimeout(timer);
      this._pendingShareRequest = null;
      reject(new Error(message));
    } else {
      showToast(message, 'warning');
    }
  }

  handleScreenShareState(payload = {}) {
    this.activeScreenSharerId = payload.active_screen_sharer_id ?? null;
    this.activeScreenSharerUsername = payload.active_screen_sharer_username ?? null;
    this.activeScreenSharerSid = payload.active_screen_sharer_sid || '';
    this.updateLocalMediaUi();
    this.notifyParticipants();
  }

  async handleScreenShareRevoked(payload = {}) {
    if (this.isScreenSharing) {
      await this.stopScreenShare({ notifyServer: false, quiet: true });
    }
    showToast(payload.message || 'Your screen share was stopped by the teacher.', 'warning');
  }

  requestServerScreenShareLock() {
    if (this._pendingShareRequest) {
      clearTimeout(this._pendingShareRequest.timer);
      this._pendingShareRequest = null;
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this._pendingShareRequest) {
          this._pendingShareRequest = null;
          reject(new Error('Screen share request timed out.'));
        }
      }, 5000);
      this._pendingShareRequest = { resolve, reject, timer };
      if (!this.socketClient.emit('request_screen_share', {})) {
        clearTimeout(timer);
        this._pendingShareRequest = null;
        reject(new Error('Real-time connection required to share screeeeeeeeeeeen.'));
      }
    });
  }

  async toggleScreenShare() {
    if (this.isScreenSharing) {
      await this.stopScreenShare();
      return;
    }

    const someoneElseSharing = Boolean(this.activeScreenSharerSid && this.activeScreenSharerSid !== this.localSid);
    if (!this.isTeacher && someoneElseSharing) {
      showToast('Another participant is already sharing their screen.', 'warning');
      return;
    }
    if (!this.isTeacher && !this.allowStudentScreenshare) {
      showToast('Screen sharing is restricted by the teacher.', 'warning');
      return;
    }

    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getDisplayMedia !== 'function') {
      showToast('Screen sharing is not supported in this browser.', 'error');
      return;
    }

    try {
      await this.requestServerScreenShareLock();
    } catch (err) {
      showToast(err.message || 'Screen sharing is not permitted.', 'warning');
      return;
    }

    try {
      this.screenStream = await navigator.mediaDevices.getDisplayMedia({
        video: { cursor: 'always' },
        audio: false,
      });
    } catch (err) {
      this.socketClient.emit('stop_screen_share', {});
      if (err?.name !== 'AbortError') {
        showToast('Screen sharing permission was denied or cancelled.', 'warning');
      }
      return;
    }

    const screenTrack = this.screenStream.getVideoTracks()[0];
    if (!screenTrack) {
      this.socketClient.emit('stop_screen_share', {});
      return;
    }

    this.isScreenSharing = true;
    screenTrack.onended = () => {
      this.stopScreenShare();
    };

    await this.replaceVideoTrackForPeers(screenTrack);

    const localVideo = document.getElementById('local-video');
    const localTile = document.getElementById('local-video-tile');
    if (localVideo) {
      localVideo.srcObject = this.screenStream;
    }
    localTile?.classList.add('is-screen');

    this.updateLocalMediaUi();
    this.broadcastMediaState();
    showToast('Screen sharing started.', 'success');
  }

  async stopScreenShare({ notifyServer = true, quiet = false } = {}) {
    if (!this.isScreenSharing) return;
    this.isScreenSharing = false;

    if (this.screenStream) {
      this.screenStream.getTracks().forEach((t) => t.stop());
      this.screenStream = null;
    }

    await this.replaceVideoTrackForPeers(this.cameraTrack || null);

    const localVideo = document.getElementById('local-video');
    const localTile = document.getElementById('local-video-tile');
    if (localVideo) {
      localVideo.srcObject = this.localStream;
    }
    localTile?.classList.remove('is-screen');

    if (notifyServer) {
      this.socketClient.emit('stop_screen_share', {});
    }

    this.updateLocalMediaUi();
    this.broadcastMediaState();
    if (!quiet) {
      showToast('Screen sharing stopped; camera restored.', 'info');
    }
  }

  async replaceVideoTrackForPeers(newTrack) {
    for (const [sid, entry] of this.peers.entries()) {
      const sender = entry.pc.getSenders().find((candidate) => candidate.track?.kind === 'video')
        || entry.pc.getTransceivers?.().find(
          (transceiver) => transceiver.sender && transceiver.receiver?.track?.kind === 'video',
        )?.sender;
      if (sender) {
        await sender.replaceTrack(newTrack);
      } else if (newTrack && this.localStream) {
        entry.pc.addTrack(newTrack, this.localStream);
        await this.sendOffer(sid);
      }
    }
  }

  broadcastMediaState() {
    this.socketClient.emit('media_state_change', {
      mic_enabled: this.micEnabled,
      cam_enabled: this.camEnabled,
      screen_sharing: this.isScreenSharing,
    });
    this.notifyParticipants();
  }

  updateLocalMediaUi() {
    const fallback = document.getElementById('local-avatar-fallback');
    const flags = document.getElementById('local-flags-label');
    const micBtn = document.getElementById('toggle-mic-btn');
    const micLabel = document.getElementById('mic-btn-label');
    const camBtn = document.getElementById('toggle-cam-btn');
    const camLabel = document.getElementById('cam-btn-label');
    const scrBtn = document.getElementById('toggle-screen-btn');
    const scrLabel = document.getElementById('screen-btn-label');
    const localTile = document.getElementById('local-video-tile');

    const showVideo = this.isScreenSharing || (this.camEnabled && Boolean(this.cameraTrack));
    fallback?.classList.toggle('hidden', showVideo);
    localTile?.classList.toggle('is-video-off', !showVideo);

    if (flags) {
      const micTxt = this.micEnabled ? 'Mic On' : 'Muted';
      const camTxt = this.isScreenSharing ? 'Sharing Screen' : (this.camEnabled ? 'Cam On' : 'Cam Off');
      flags.textContent = `${micTxt} · ${camTxt}`;
    }

    if (micBtn && micLabel) {
      const micLocked = this.isMicLockedForMe();
      micBtn.classList.toggle('is-off', !this.micEnabled || micLocked);
      micBtn.disabled = micLocked;
      micLabel.textContent = micLocked ? 'Mic Locked' : (this.micEnabled ? 'Mute Mic' : 'Unmute Mic');
    }
    if (camBtn && camLabel) {
      const camLocked = this.isCamLockedForMe();
      camBtn.classList.toggle('is-off', !this.camEnabled || camLocked);
      camBtn.disabled = camLocked;
      camLabel.textContent = camLocked ? 'Cam Locked' : (this.camEnabled ? 'Turn Off Cam' : 'Turn On Cam');
    }
    const localHandBadge = document.getElementById('local-hand-badge');
    if (localHandBadge) {
      localHandBadge.classList.toggle('hidden', !this.handRaised);
    }
    const handBtn = document.getElementById('toggle-hand-btn');
    const handLabel = document.getElementById('hand-btn-label');
    if (handBtn && handLabel) {
      handBtn.classList.toggle('is-active', this.handRaised);
      handLabel.textContent = this.handRaised ? 'Lower Hand' : 'Raise Hand';
    }
    if (scrBtn && scrLabel) {
      const otherSharing = Boolean(this.activeScreenSharerSid && this.activeScreenSharerSid !== this.localSid);
      const restricted = !this.isTeacher && (!this.allowStudentScreenshare || otherSharing);
      scrBtn.classList.toggle('is-active', this.isScreenSharing);
      scrBtn.disabled = !this.isScreenSharing && restricted;
      if (this.isScreenSharing) {
        scrLabel.textContent = 'Stop Sharing';
      } else if (otherSharing && !this.isTeacher) {
        scrLabel.textContent = 'Screen In Use';
      } else if (!this.isTeacher && !this.allowStudentScreenshare) {
        scrLabel.textContent = 'Share Restricted';
      } else {
        scrLabel.textContent = 'Share Screen';
      }
    }
  }

  createRemoteTile(sid, entry) {
    const grid = document.getElementById('video-stage');
    if (!grid) return;

    const tile = document.createElement('article');
    tile.className = 'video-tile';
    tile.id = `remote-tile-${sid}`;

    const video = document.createElement('video');
    video.autoplay = true;
    video.playsInline = true;
    video.srcObject = entry.stream;

    const fallback = document.createElement('div');
    fallback.className = 'video-avatar-fallback hidden';
    const circle = document.createElement('div');
    circle.className = 'avatar-circle';
    circle.textContent = getInitials(entry.participant.full_name || entry.participant.username);
    const fname = document.createElement('span');
    fname.textContent = entry.participant.full_name || entry.participant.username;
    fallback.append(circle, fname);

    const handBadge = document.createElement('span');
    handBadge.className = 'video-hand-badge hidden';
    handBadge.textContent = '✋ Hand Raised';

    const overlay = document.createElement('div');
    overlay.className = 'video-overlay-bar';
    const nameEl = document.createElement('span');
    nameEl.className = 'video-participant-name';
    nameEl.textContent = entry.participant.full_name || entry.participant.username;
    const flagsEl = document.createElement('span');
    flagsEl.className = 'video-media-flags';
    overlay.append(nameEl, flagsEl);

    tile.append(video, fallback, handBadge, overlay);
    grid.appendChild(tile);

    entry.tileEl = tile;
    entry.videoEl = video;
    entry.fallbackEl = fallback;
    entry.flagsEl = flagsEl;
    entry.handBadgeEl = handBadge;

    this.updateRemoteTileUi(sid);
  }

  updateRemoteTileUi(sid) {
    const entry = this.peers.get(sid);
    if (!entry || !entry.tileEl) return;

    const p = entry.participant;
    const hasVideoTrack = entry.stream && entry.stream.getVideoTracks().some((t) => t.readyState === 'live');
    const showVideo = p.screen_sharing || (p.cam_enabled !== false && hasVideoTrack);

    entry.fallbackEl?.classList.toggle('hidden', Boolean(showVideo));
    entry.tileEl.classList.toggle('is-screen', Boolean(p.screen_sharing));
    entry.tileEl.classList.toggle('is-video-off', !showVideo);
    entry.handBadgeEl?.classList.toggle('hidden', !p.hand_raised);

    if (entry.flagsEl) {
      const micTxt = p.mic_locked ? 'Mic Locked' : (p.mic_enabled === false ? 'Muted' : 'Mic On');
      const camTxt = p.screen_sharing ? 'Sharing Screen' : (p.camera_locked ? 'Cam Locked' : (p.cam_enabled === false ? 'Cam Off' : 'Cam On'));
      const chatTxt = p.chat_muted ? ' · Chat Muted' : '';
      entry.flagsEl.textContent = `${micTxt} · ${camTxt}${chatTxt}`;
    }
  }

  getParticipantList() {
    const list = [
      {
        sid: this.localSid || 'local',
        user_id: this.localUser.id,
        username: this.localUser.username,
        full_name: `${this.localUser.full_name || this.localUser.username} (You)`,
        is_host: this.isTeacher,
        meeting_role: this.isTeacher ? 'teacher' : 'student',
        mic_enabled: this.micEnabled,
        cam_enabled: this.camEnabled,
        mic_locked: this.isMicLockedForMe(),
        camera_locked: this.isCamLockedForMe(),
        hand_raised: this.handRaised,
        screen_sharing: this.isScreenSharing,
      },
    ];
    for (const entry of this.peers.values()) {
      list.push(entry.participant);
    }
    return list;
  }

  notifyParticipants() {
    this.onParticipantsChanged?.(this.getParticipantList());
  }

  cleanup() {
    this.isCleaningUp = true;
    for (const sid of Array.from(this.peers.keys())) {
      this.removePeer(sid);
    }
    if (this.screenStream) {
      this.screenStream.getTracks().forEach((t) => t.stop());
      this.screenStream = null;
    }
    if (this.localStream) {
      this.localStream.getTracks().forEach((t) => t.stop());
      this.localStream = null;
    }
  }
}
