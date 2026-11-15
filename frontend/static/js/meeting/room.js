import { apiRequest } from '../core/api.js';
import { requireAuth } from '../core/auth.js';
import { copyToClipboard, showToast } from '../core/ui.js';
import { MeetingFileShare } from './fileshare.js';
import { MeetingSocketClient } from './socket-client.js';
import { PeerConnectionManager } from './webrtc.js';
import { CollaborativeWhiteboard } from './whiteboard.js';

function setSocketStatus(state) {
  const indicator = document.getElementById('socket-indicator');
  const text = document.getElementById('socket-status-text');
  if (!indicator || !text) return;

  indicator.classList.remove('is-connected', 'is-reconnecting');
  if (state === 'connected') {
    indicator.classList.add('is-connected');
    text.textContent = 'Connected';
  } else if (state === 'reconnecting') {
    indicator.classList.add('is-reconnecting');
    text.textContent = 'Reconnecting...';
  } else {
    text.textContent = 'Disconnected';
  }
}

function renderParticipantsList(participants) {
  const countLabel = document.getElementById('participant-count-label');
  const listEl = document.getElementById('participants-list');
  if (countLabel) {
    countLabel.textContent = `${participants.length} ${participants.length === 1 ? 'participant' : 'participants'}`;
  }
  if (!listEl) return;

  listEl.innerHTML = '';
  participants.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'participant-row';

    const info = document.createElement('div');
    info.className = 'participant-info';
    const name = document.createElement('div');
    name.className = 'participant-name';
    name.textContent = p.full_name || p.username;
    const role = document.createElement('div');
    role.className = 'participant-role';
    const micTxt = p.mic_enabled === false ? 'Muted' : 'Mic On';
    const camTxt = p.screen_sharing ? 'Sharing Screen' : (p.cam_enabled === false ? 'Cam Off' : 'Cam On');
    role.textContent = `${p.is_host ? 'Host · ' : ''}${micTxt} · ${camTxt}`;
    info.append(name, role);

    row.appendChild(info);
    listEl.appendChild(row);
  });
}

async function initMeetingRoom() {
  const user = await requireAuth();
  if (!user) return;

  const shell = document.querySelector('.meeting-shell');
  const meetingCode = shell?.getAttribute('data-meeting-code');
  if (!meetingCode) {
    showToast('Invalid meeting code.', 'error');
    window.location.replace('/dashboard/');
    return;
  }

  let meeting;
  try {
    meeting = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/join/`, {
      method: 'POST',
    });
  } catch (err) {
    showToast(err.message || 'Unable to join meeting.', 'error');
    setTimeout(() => window.location.replace('/dashboard/'), 1500);
    return;
  }

  document.getElementById('room-meeting-title').textContent = meeting.title;
  document.getElementById('room-meeting-code').textContent = meeting.meeting_code;

  const endBtn = document.getElementById('end-meeting-btn');
  if (meeting.is_host && endBtn) {
    endBtn.classList.remove('hidden');
  }

  let iceServers = [{ urls: 'stun:stun.l.google.com:19302' }];
  try {
    const iceRes = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/ice-servers/`);
    if (Array.isArray(iceRes.ice_servers) && iceRes.ice_servers.length) {
      iceServers = iceRes.ice_servers;
    }
  } catch {
    // Fallback to public STUN
  }

  let peerManager = null;
  let whiteboard = null;
  let fileShare = null;

  const socketClient = new MeetingSocketClient(meetingCode, {
    onConnectionState: (state) => setSocketStatus(state),
    onError: (msg) => showToast(msg, 'error'),
    onRoomJoined: async (payload) => {
      if (peerManager) {
        peerManager.setLocalSid(payload.self_sid);
        await peerManager.syncRoomPeers(payload.peers || []);
      }
      if (whiteboard && Array.isArray(payload.whiteboard_ops)) {
        whiteboard.loadState(payload.whiteboard_ops);
      }
    },
    onParticipantJoined: async (payload) => {
      if (peerManager && payload?.participant) {
        await peerManager.handleParticipantJoined(payload.participant);
        showToast(`${payload.participant.full_name || payload.participant.username} joined.`, 'info');
      }
    },
    onParticipantLeft: (payload) => {
      if (peerManager && payload?.sid) {
        peerManager.removePeer(payload.sid);
      }
    },
    onMediaState: (payload) => peerManager?.handleRemoteMediaState(payload),
    onMeetingEnded: () => {
      showToast('This meeting was ended by the host.', 'warning');
      peerManager?.cleanup();
      setTimeout(() => window.location.replace('/dashboard/'), 1200);
    },
    onWebRtcOffer: (payload) => peerManager?.handleOffer(payload),
    onWebRtcAnswer: (payload) => peerManager?.handleAnswer(payload),
    onWebRtcIce: (payload) => peerManager?.handleIceCandidate(payload),
    onWhiteboardState: (payload) => whiteboard?.loadState(payload.operations || []),
    onWhiteboardDraw: (payload) => whiteboard?.applyOperation(payload, true),
    onWhiteboardText: (payload) => whiteboard?.applyOperation(payload, true),
    onWhiteboardClear: () => whiteboard?.clearLocal(),
    onFileShared: (payload) => fileShare?.addRemoteFile(payload?.file),
  });

  peerManager = new PeerConnectionManager({
    socketClient,
    iceServers,
    localUser: user,
    onParticipantsChanged: renderParticipantsList,
  });

  await peerManager.initLocalMedia();
  renderParticipantsList(peerManager.getParticipantList());

  whiteboard = new CollaborativeWhiteboard({
    canvasId: 'whiteboard-canvas',
    wrapId: 'wb-canvas-wrap',
    socketClient,
  });

  fileShare = new MeetingFileShare(meetingCode, socketClient);
  await fileShare.loadFiles();

  socketClient.connect();

  // Stage View Switching (Video Grid vs Whiteboard)
  const videoStage = document.getElementById('video-stage');
  const wbStage = document.getElementById('whiteboard-stage');
  const toggleWbBtn = document.getElementById('toggle-wb-btn');

  function setStageView(view) {
    const isWb = view === 'whiteboard';
    videoStage.classList.toggle('hidden', isWb);
    wbStage.classList.toggle('hidden', !isWb);
    document.querySelectorAll('[data-stage-view]').forEach((t) => {
      t.classList.toggle('is-active', t.getAttribute('data-stage-view') === view);
    });
    toggleWbBtn?.classList.toggle('is-active', isWb);
    if (isWb) {
      requestAnimationFrame(() => whiteboard.resizeCanvas());
    }
  }

  document.querySelectorAll('[data-stage-view]').forEach((tab) => {
    tab.addEventListener('click', () => setStageView(tab.getAttribute('data-stage-view')));
  });
  toggleWbBtn?.addEventListener('click', () => {
    const currentlyWb = !wbStage.classList.contains('hidden');
    setStageView(currentlyWb ? 'video' : 'whiteboard');
  });

  // Side Drawer Switching (Participants vs Shared Files)
  const workspace = document.getElementById('meeting-workspace');
  const drawer = document.getElementById('meeting-drawer');
  const partPanel = document.getElementById('drawer-participants-panel');
  const filesPanel = document.getElementById('drawer-files-panel');

  function openDrawer(tab) {
    workspace.classList.add('has-drawer');
    drawer.classList.remove('hidden');
    partPanel.classList.toggle('hidden', tab !== 'participants');
    filesPanel.classList.toggle('hidden', tab !== 'files');
    document.querySelectorAll('[data-drawer-tab]').forEach((t) => {
      t.classList.toggle('is-active', t.getAttribute('data-drawer-tab') === tab);
    });
    setTimeout(() => whiteboard.resizeCanvas(), 50);
  }

  function closeDrawer() {
    workspace.classList.remove('has-drawer');
    drawer.classList.add('hidden');
    setTimeout(() => whiteboard.resizeCanvas(), 50);
  }

  document.querySelectorAll('[data-drawer-tab]').forEach((tab) => {
    tab.addEventListener('click', () => openDrawer(tab.getAttribute('data-drawer-tab')));
  });
  document.getElementById('close-drawer-btn')?.addEventListener('click', closeDrawer);
  document.getElementById('toggle-participants-btn')?.addEventListener('click', () => {
    const isOpen = !drawer.classList.contains('hidden') && !partPanel.classList.contains('hidden');
    if (isOpen) closeDrawer(); else openDrawer('participants');
  });
  document.getElementById('toggle-files-btn')?.addEventListener('click', () => {
    const isOpen = !drawer.classList.contains('hidden') && !filesPanel.classList.contains('hidden');
    if (isOpen) closeDrawer(); else openDrawer('files');
  });

  // Media Controls
  document.getElementById('toggle-mic-btn')?.addEventListener('click', () => peerManager.toggleMic());
  document.getElementById('toggle-cam-btn')?.addEventListener('click', () => peerManager.toggleCamera());
  document.getElementById('toggle-screen-btn')?.addEventListener('click', () => peerManager.toggleScreenShare());

  document.getElementById('copy-invite-btn')?.addEventListener('click', async () => {
    const link = `${window.location.origin}/meeting/${meetingCode}/`;
    await copyToClipboard(link);
    showToast('Meeting invitation link copied.', 'success');
  });

  document.getElementById('leave-meeting-btn')?.addEventListener('click', async () => {
    try {
      await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/leave/`, { method: 'POST' });
    } catch {
      // Proceed with cleanup
    }
    socketClient.disconnect();
    peerManager.cleanup();
    window.location.replace('/dashboard/');
  });

  endBtn?.addEventListener('click', async () => {
    endBtn.disabled = true;
    try {
      await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/end/`, { method: 'POST' });
      socketClient.emit('end_meeting', {});
      socketClient.disconnect();
      peerManager.cleanup();
      showToast('Meeting ended.', 'success');
      window.location.replace('/dashboard/');
    } catch (err) {
      showToast(err.message, 'error');
      endBtn.disabled = false;
    }
  });

  // Deep-linked query params (?view=whiteboard or ?drawer=files)
  const params = new URLSearchParams(window.location.search);
  if (params.get('view') === 'whiteboard') setStageView('whiteboard');
  if (params.get('drawer') === 'files') openDrawer('files');

  window.addEventListener('beforeunload', () => {
    socketClient.disconnect();
    peerManager.cleanup();
  });
}

document.addEventListener('DOMContentLoaded', initMeetingRoom);
