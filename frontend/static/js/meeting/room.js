import { apiRequest } from '../core/api.js';
import { requireAuth } from '../core/auth.js';
import { copyToClipboard, showToast } from '../core/ui.js';
import { MeetingFileShare } from './fileshare.js';
import { MeetingSocketClient } from './socket-client.js';
import { PeerConnectionManager } from './webrtc.js';
import { CollaborativeWhiteboard } from './whiteboard.js';

function setSocketStatus(state, joined = false) {
  const indicator = document.getElementById('socket-indicator');
  const text = document.getElementById('socket-status-text');
  if (indicator && text) {
    indicator.classList.remove('is-connected', 'is-reconnecting');
    if (state === 'connected' && joined) {
      indicator.classList.add('is-connected');
      text.textContent = 'Connected';
    } else if (state === 'connected' || state === 'reconnecting') {
      indicator.classList.add('is-reconnecting');
      text.textContent = state === 'connected' ? 'Joining room…' : 'Reconnecting…';
    } else {
      text.textContent = 'Disconnected';
    }
  }

  const chatStatus = document.getElementById('chat-connection-status');
  if (chatStatus) {
    chatStatus.textContent = state === 'connected'
      ? joined ? 'Connected' : 'Joining room…'
      : state === 'reconnecting' ? 'Reconnecting…' : 'Disconnected';
  }
  const sendButton = document.getElementById('chat-send-btn');
  if (sendButton) sendButton.disabled = state !== 'connected' || !joined;
}

function renderChatMessage(payload, user) {
  const messages = document.getElementById('chat-messages');
  if (!messages || !payload?.text) return;

  const sender = document.createElement('div');
  const ownMessage = String(payload.sender_id) === String(user.id)
    || payload.sender_username === user.username;
  sender.className = `chat-message${ownMessage ? ' is-own' : ''}`;

  const meta = document.createElement('div');
  meta.className = 'chat-message-meta';
  const name = document.createElement('span');
  const senderName = payload.sender_name || payload.sender_username || 'Participant';
  name.textContent = ownMessage ? `${senderName} (You)` : senderName;
  const time = document.createElement('time');
  const date = new Date(payload.timestamp);
  time.textContent = Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  meta.append(name, time);

  const text = document.createElement('div');
  text.className = 'chat-message-text';
  text.textContent = payload.text;
  sender.append(meta, text);
  messages.appendChild(sender);
  messages.scrollTop = messages.scrollHeight;
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

function setupDrawerControls(resizeWhiteboard) {
  const workspace = document.getElementById('meeting-workspace');
  const drawer = document.getElementById('meeting-drawer');
  const partPanel = document.getElementById('drawer-participants-panel');
  const filesPanel = document.getElementById('drawer-files-panel');
  const chatPanel = document.getElementById('drawer-chat-panel');
  let drawerCloseTimeout = null;

  function openDrawer(tab) {
    if (drawerCloseTimeout) {
      clearTimeout(drawerCloseTimeout);
      drawerCloseTimeout = null;
    }
    workspace.classList.add('has-drawer');
    drawer.classList.remove('hidden');
    drawer.setAttribute('aria-hidden', 'false');
    drawer.getBoundingClientRect();
    drawer.classList.toggle('is-chat-open', tab === 'chat');
    partPanel.classList.toggle('hidden', tab !== 'participants');
    filesPanel.classList.toggle('hidden', tab !== 'files');
    chatPanel.classList.toggle('hidden', tab !== 'chat');
    document.querySelectorAll('[data-drawer-tab]').forEach((tabButton) => {
      tabButton.classList.toggle('is-active', tabButton.getAttribute('data-drawer-tab') === tab);
    });
    document.getElementById('toggle-chat-btn')?.classList.toggle('is-active', tab === 'chat');
    document.getElementById('toggle-chat-btn')?.setAttribute('aria-expanded', String(tab === 'chat'));
    requestAnimationFrame(() => {
      if (!drawer.classList.contains('hidden')) drawer.classList.add('is-open');
    });
    if (tab === 'chat') {
      const messages = document.getElementById('chat-messages');
      if (messages) messages.scrollTop = messages.scrollHeight;
    }
    setTimeout(resizeWhiteboard, 50);
  }

  function finishClosingDrawer() {
    if (drawer.classList.contains('is-open')) return;
    drawer.classList.add('hidden');
    workspace.classList.remove('has-drawer');
    drawer.setAttribute('aria-hidden', 'true');
    drawerCloseTimeout = null;
    setTimeout(resizeWhiteboard, 50);
  }

  function closeDrawer() {
    if (drawer.classList.contains('hidden')) return;
    drawer.classList.remove('is-open', 'is-chat-open');
    document.getElementById('toggle-chat-btn')?.classList.remove('is-active');
    document.getElementById('toggle-chat-btn')?.setAttribute('aria-expanded', 'false');
    drawer.addEventListener('transitionend', finishClosingDrawer, { once: true });
    drawerCloseTimeout = setTimeout(finishClosingDrawer, 260);
  }

  document.querySelectorAll('[data-drawer-tab]').forEach((tab) => {
    tab.addEventListener('click', () => openDrawer(tab.getAttribute('data-drawer-tab')));
  });
  document.getElementById('close-drawer-btn')?.addEventListener('click', closeDrawer);
  document.getElementById('close-chat-btn')?.addEventListener('click', closeDrawer);
  document.getElementById('toggle-participants-btn')?.addEventListener('click', () => {
    const isOpen = drawer.classList.contains('is-open') && !partPanel.classList.contains('hidden');
    if (isOpen) closeDrawer(); else openDrawer('participants');
  });
  document.getElementById('toggle-files-btn')?.addEventListener('click', () => {
    const isOpen = drawer.classList.contains('is-open') && !filesPanel.classList.contains('hidden');
    if (isOpen) closeDrawer(); else openDrawer('files');
  });
  document.getElementById('toggle-chat-btn')?.addEventListener('click', () => {
    const isOpen = drawer.classList.contains('is-open') && !chatPanel.classList.contains('hidden');
    if (isOpen) closeDrawer(); else openDrawer('chat');
  });
}

function setupChatForm(socketClient) {
  const chatForm = document.getElementById('chat-form');
  const chatInput = document.getElementById('chat-message-input');
  chatInput?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      chatForm?.requestSubmit();
    }
  });
  chatForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = chatInput?.value.trim();
    if (!text || !socketClient.hasJoined) return;
    if (socketClient.emit('chat_message', { text: text.slice(0, 2000) })) {
      chatInput.value = '';
      chatInput.focus();
    }
  });
}

function setupStageViewControls(whiteboard) {
  const videoStage = document.getElementById('video-stage');
  const wbStage = document.getElementById('whiteboard-stage');
  const toggleWbBtn = document.getElementById('toggle-wb-btn');

  function setStageView(view) {
    const isWb = view === 'whiteboard';
    videoStage.classList.toggle('hidden', isWb);
    wbStage.classList.toggle('hidden', !isWb);
    document.querySelectorAll('[data-stage-view]').forEach((tab) => {
      tab.classList.toggle('is-active', tab.getAttribute('data-stage-view') === view);
    });
    toggleWbBtn?.classList.toggle('is-active', isWb);
    if (isWb) requestAnimationFrame(() => whiteboard.resizeCanvas());
  }

  document.querySelectorAll('[data-stage-view]').forEach((tab) => {
    tab.addEventListener('click', () => setStageView(tab.getAttribute('data-stage-view')));
  });
  toggleWbBtn?.addEventListener('click', () => {
    const currentlyWb = !wbStage.classList.contains('hidden');
    setStageView(currentlyWb ? 'video' : 'whiteboard');
  });

  return setStageView;
}

async function initMeetingRoom() {
  let whiteboard = null;
  setupDrawerControls(() => whiteboard?.resizeCanvas());

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
  let fileShare = null;
  let mediaReady = false;
  let pendingRoomPeers = [];
  const pendingParticipants = new Map();
  const pendingPeerActions = [];
  const departedPeerSids = new Set();

  function runPeerAction(action, sid) {
    if (sid && departedPeerSids.has(sid)) return;
    const guardedAction = () => {
      if (sid && departedPeerSids.has(sid)) return;
      return action();
    };
    if (mediaReady) return guardedAction();
    pendingPeerActions.push(guardedAction);
  }

  const socketClient = new MeetingSocketClient(meetingCode, {
    onConnectionState: (state) => {
      setSocketStatus(state, socketClient.hasJoined);
      whiteboard?.setEnabled(state === 'connected' && socketClient.hasJoined);
    },
    onError: (msg) => {
      setSocketStatus('disconnected');
      whiteboard?.setEnabled(false);
      showToast(msg, 'error');
    },
    onRoomJoined: async (payload) => {
      setSocketStatus('connected', true);
      if (whiteboard && Array.isArray(payload.whiteboard_ops)) {
        whiteboard.loadState(payload.whiteboard_ops);
        whiteboard.setEnabled(true);
      }
      if (peerManager) {
        peerManager.setLocalSid(payload.self_sid);
        for (const participant of payload.peers || []) {
          departedPeerSids.delete(participant.sid);
        }
        if (mediaReady) {
          await peerManager.syncRoomPeers(payload.peers || []);
        } else {
          pendingRoomPeers = payload.peers || [];
        }
      }
    },
    onParticipantJoined: async (payload) => {
      if (peerManager && payload?.participant) {
        departedPeerSids.delete(payload.participant.sid);
        if (!mediaReady) {
          pendingParticipants.set(payload.participant.sid, payload.participant);
          return;
        }
        await peerManager.handleParticipantJoined(payload.participant);
        showToast(`${payload.participant.full_name || payload.participant.username} joined.`, 'info');
      }
    },
    onParticipantLeft: (payload) => {
      if (payload?.sid) departedPeerSids.add(payload.sid);
      if (payload?.sid && !mediaReady) {
        pendingParticipants.delete(payload.sid);
        pendingRoomPeers = pendingRoomPeers.filter((participant) => participant.sid !== payload.sid);
      } else if (peerManager && payload?.sid) {
        peerManager.removePeer(payload.sid);
      }
    },
    onMediaState: (payload) => runPeerAction(
      () => peerManager?.handleRemoteMediaState(payload),
      payload?.sid,
    ),
    onMeetingEnded: () => {
      showToast('This meeting was ended by the host.', 'warning');
      peerManager?.cleanup();
      setTimeout(() => window.location.replace('/dashboard/'), 1200);
    },
    onWebRtcOffer: (payload) => runPeerAction(() => peerManager?.handleOffer(payload), payload?.from_sid),
    onWebRtcAnswer: (payload) => runPeerAction(() => peerManager?.handleAnswer(payload), payload?.from_sid),
    onWebRtcIce: (payload) => runPeerAction(() => peerManager?.handleIceCandidate(payload), payload?.from_sid),
    onWhiteboardState: (payload) => whiteboard?.loadState(payload.operations || []),
    onWhiteboardDraw: (payload) => whiteboard?.applyOperation(payload, true),
    onWhiteboardText: (payload) => whiteboard?.applyOperation(payload, true),
    onWhiteboardClear: () => whiteboard?.clearLocal(),
    onChatMessage: (payload) => renderChatMessage(payload, user),
    onFileShared: (payload) => fileShare?.addRemoteFile(payload?.file),
  });

  peerManager = new PeerConnectionManager({
    socketClient,
    iceServers,
    localUser: user,
    onParticipantsChanged: renderParticipantsList,
  });

  whiteboard = new CollaborativeWhiteboard({
    canvasId: 'whiteboard-canvas',
    wrapId: 'wb-canvas-wrap',
    socketClient,
  });

  fileShare = new MeetingFileShare(meetingCode, socketClient);
  const setStageView = setupStageViewControls(whiteboard);
  setupChatForm(socketClient);

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
  if (params.get('drawer') === 'files') {
    document.querySelector('[data-drawer-tab="files"]')?.click();
  }
  if (params.get('drawer') === 'chat') {
    document.querySelector('[data-drawer-tab="chat"]')?.click();
  }

  const mediaPromise = peerManager.initLocalMedia();
  try {
    socketClient.connect();
  } catch (err) {
    setSocketStatus('disconnected');
    showToast(err.message || 'Real-time connection could not be started.', 'error');
  }
  const filesPromise = fileShare.loadFiles();

  await mediaPromise;
  mediaReady = true;
  await peerManager.syncRoomPeers(pendingRoomPeers);
  pendingRoomPeers = [];
  for (const participant of pendingParticipants.values()) {
    await peerManager.handleParticipantJoined(participant);
  }
  pendingParticipants.clear();
  while (pendingPeerActions.length) {
    await pendingPeerActions.shift()();
  }
  renderParticipantsList(peerManager.getParticipantList());
  await filesPromise;

  window.addEventListener('beforeunload', () => {
    socketClient.disconnect();
    peerManager.cleanup();
  });
}

document.addEventListener('DOMContentLoaded', initMeetingRoom);
