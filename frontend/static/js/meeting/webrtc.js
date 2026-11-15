import { getInitials, showToast } from '../core/ui.js';

export class PeerConnectionManager {
  constructor({ socketClient, iceServers, localUser, onParticipantsChanged }) {
    this.socketClient = socketClient;
    this.iceServers = iceServers || [{ urls: 'stun:stun.l.google.com:19302' }];
    this.localUser = localUser;
    this.onParticipantsChanged = onParticipantsChanged;

    this.localSid = null;
    this.localStream = null;
    this.cameraTrack = null;
    this.screenStream = null;

    this.micEnabled = true;
    this.camEnabled = true;
    this.isScreenSharing = false;

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

    try {
      this.localStream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      this.cameraTrack = this.localStream.getVideoTracks()[0] || null;
      this.micEnabled = Boolean(this.localStream.getAudioTracks().length);
      this.camEnabled = Boolean(this.cameraTrack);
      if (localVideo) {
        localVideo.srcObject = this.localStream;
      }
    } catch {
      try {
        this.localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        this.micEnabled = Boolean(this.localStream.getAudioTracks().length);
        this.camEnabled = false;
        showToast('Camera unavailable; joined with microphone only.', 'warning');
      } catch {
        this.localStream = new MediaStream();
        this.micEnabled = false;
        this.camEnabled = false;
        showToast('Camera and microphone permissions denied or unavailable. Joined in listen-only mode.', 'warning');
      }
    }

    this.updateLocalMediaUi();
  }

  setLocalSid(sid) {
    this.localSid = sid;
  }

  async syncRoomPeers(peersList) {
    for (const peerInfo of peersList) {
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
      existing.participant = { ...existing.participant, ...peerInfo };
      this.updateRemoteTileUi(sid);
      return existing;
    }

    const pc = new RTCPeerConnection({ iceServers: this.iceServers });
    const remoteStream = new MediaStream();
    const peerEntry = {
      pc,
      participant: peerInfo,
      pendingIce: [],
      stream: remoteStream,
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
      if (event.streams && event.streams[0]) {
        peerEntry.stream = event.streams[0];
        if (peerEntry.videoEl) {
          peerEntry.videoEl.srcObject = event.streams[0];
        }
      } else {
        peerEntry.stream.addTrack(event.track);
        if (peerEntry.videoEl) {
          peerEntry.videoEl.srcObject = peerEntry.stream;
        }
      }
      this.updateRemoteTileUi(sid);
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

  handleRemoteMediaState({ sid, mic_enabled, cam_enabled, screen_sharing }) {
    const entry = this.peers.get(sid);
    if (!entry) return;
    entry.participant.mic_enabled = mic_enabled;
    entry.participant.cam_enabled = cam_enabled;
    entry.participant.screen_sharing = screen_sharing;
    this.updateRemoteTileUi(sid);
    this.notifyParticipants();
  }

  removePeer(sid) {
    const entry = this.peers.get(sid);
    if (!entry) return;
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

  toggleMic() {
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

  toggleCamera() {
    if (this.isScreenSharing) {
      showToast('Stop screen sharing before toggling camera.', 'warning');
      return;
    }
    if (!this.cameraTrack) {
      showToast('No camera track available.', 'warning');
      return;
    }
    this.camEnabled = !this.camEnabled;
    this.cameraTrack.enabled = this.camEnabled;
    this.updateLocalMediaUi();
    this.broadcastMediaState();
  }

  async toggleScreenShare() {
    if (this.isScreenSharing) {
      await this.stopScreenShare();
      return;
    }

    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getDisplayMedia !== 'function') {
      showToast('Screen sharing is not supported in this browser.', 'error');
      return;
    }

    try {
      this.screenStream = await navigator.mediaDevices.getDisplayMedia({
        video: { cursor: 'always' },
        audio: false,
      });
    } catch (err) {
      if (err?.name !== 'AbortError') {
        showToast('Screen sharing permission was denied or cancelled.', 'warning');
      }
      return;
    }

    const screenTrack = this.screenStream.getVideoTracks()[0];
    if (!screenTrack) return;

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

  async stopScreenShare() {
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

    this.updateLocalMediaUi();
    this.broadcastMediaState();
    showToast('Screen sharing stopped; camera restored.', 'info');
  }

  async replaceVideoTrackForPeers(newTrack) {
    for (const [sid, entry] of this.peers.entries()) {
      const sender = entry.pc.getSenders().find((s) => s.track && s.track.kind === 'video');
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

    const showVideo = this.isScreenSharing || (this.camEnabled && Boolean(this.cameraTrack));
    fallback?.classList.toggle('hidden', showVideo);

    if (flags) {
      const micTxt = this.micEnabled ? 'Mic On' : 'Muted';
      const camTxt = this.isScreenSharing ? 'Sharing Screen' : (this.camEnabled ? 'Cam On' : 'Cam Off');
      flags.textContent = `${micTxt} · ${camTxt}`;
    }

    if (micBtn && micLabel) {
      micBtn.classList.toggle('is-off', !this.micEnabled);
      micLabel.textContent = this.micEnabled ? 'Mute Mic' : 'Unmute Mic';
    }
    if (camBtn && camLabel) {
      camBtn.classList.toggle('is-off', !this.camEnabled);
      camLabel.textContent = this.camEnabled ? 'Turn Off Cam' : 'Turn On Cam';
    }
    if (scrBtn && scrLabel) {
      scrBtn.classList.toggle('is-active', this.isScreenSharing);
      scrLabel.textContent = this.isScreenSharing ? 'Stop Sharing' : 'Share Screen';
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

    const overlay = document.createElement('div');
    overlay.className = 'video-overlay-bar';
    const nameEl = document.createElement('span');
    nameEl.className = 'video-participant-name';
    nameEl.textContent = entry.participant.full_name || entry.participant.username;
    const flagsEl = document.createElement('span');
    flagsEl.className = 'video-media-flags';
    overlay.append(nameEl, flagsEl);

    tile.append(video, fallback, overlay);
    grid.appendChild(tile);

    entry.tileEl = tile;
    entry.videoEl = video;
    entry.fallbackEl = fallback;
    entry.flagsEl = flagsEl;

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

    if (entry.flagsEl) {
      const micTxt = p.mic_enabled === false ? 'Muted' : 'Mic On';
      const camTxt = p.screen_sharing ? 'Sharing Screen' : (p.cam_enabled === false ? 'Cam Off' : 'Cam On');
      entry.flagsEl.textContent = `${micTxt} · ${camTxt}`;
    }
  }

  getParticipantList() {
    const list = [
      {
        sid: this.localSid || 'local',
        username: this.localUser.username,
        full_name: `${this.localUser.full_name || this.localUser.username} (You)`,
        mic_enabled: this.micEnabled,
        cam_enabled: this.camEnabled,
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
