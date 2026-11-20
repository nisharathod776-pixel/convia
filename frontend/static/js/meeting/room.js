import { apiRequest, apiUrl, getAccessToken } from '../core/api.js';
import { requireAuth } from '../core/auth.js';
import { copyToClipboard, downloadBlobUrl, formatBytes, formatDateTime, showToast } from '../core/ui.js';
import { MeetingFileShare } from './fileshare.js';
import {
  createRecordingPlayer,
  fetchAuthorizedRecordingBlobUrl,
  MeetingSessionRecorder,
  releaseRecordingPreview,
  uploadSessionRecording,
} from './recorder.js';
import { MeetingSocketClient } from './socket-client.js';
import { PeerConnectionManager } from './webrtc.js';
import { CollaborativeWhiteboard } from './whiteboard.js';

function formatElapsedSeconds(totalSeconds) {
  const s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

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
}

function toLocalDateTimeValue(isoString) {
  if (!isoString) return '';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function showAdmissionGate({ title, message, meta = '', isError = false }) {
  const overlay = document.getElementById('admission-gate-overlay');
  const spinner = document.getElementById('admission-gate-spinner');
  const titleEl = document.getElementById('admission-gate-title');
  const msgEl = document.getElementById('admission-gate-message');
  const metaEl = document.getElementById('admission-gate-meta');
  if (!overlay) return;

  overlay.classList.remove('hidden');
  spinner?.classList.toggle('is-error', isError);
  if (titleEl) titleEl.textContent = title;
  if (msgEl) msgEl.textContent = message;
  if (metaEl) {
    metaEl.textContent = meta || '';
    metaEl.classList.toggle('hidden', !meta);
  }
}

function hideAdmissionGate() {
  document.getElementById('admission-gate-overlay')?.classList.add('hidden');
}

function removeChatMessageElement(messageId) {
  if (!messageId) return;
  const messages = document.getElementById('chat-messages');
  const el = messages?.querySelector(`[data-message-id="${CSS.escape(String(messageId))}"]`);
  el?.remove();
}

function renderChatMessage(payload, user, { isTeacher = false, onDelete = null } = {}) {
  const messages = document.getElementById('chat-messages');
  if (!messages || !payload?.text) return;

  if (payload.id && messages.querySelector(`[data-message-id="${CSS.escape(String(payload.id))}"]`)) {
    return;
  }

  const sender = document.createElement('div');
  const ownMessage = String(payload.sender_id) === String(user.id)
    || payload.sender_username === user.username;
  sender.className = `chat-message${ownMessage ? ' is-own' : ''}`;
  if (payload.id) {
    sender.setAttribute('data-message-id', String(payload.id));
  }

  const meta = document.createElement('div');
  meta.className = 'chat-message-meta';
  const name = document.createElement('span');
  const senderName = payload.sender_name || payload.sender_username || 'Participant';
  const roleSuffix = payload.sender_role === 'teacher' ? ' · Teacher' : '';
  name.textContent = ownMessage ? `${senderName} (You)${roleSuffix}` : `${senderName}${roleSuffix}`;

  const rightMeta = document.createElement('span');
  rightMeta.className = 'chat-message-meta-right';

  const time = document.createElement('time');
  const date = new Date(payload.timestamp);
  time.textContent = Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  rightMeta.appendChild(time);

  const canDelete = Boolean(
    payload.id
    && typeof onDelete === 'function'
    && (isTeacher || (ownMessage && payload.sender_role !== 'teacher')),
  );
  if (canDelete) {
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'chat-delete-btn';
    delBtn.textContent = 'Delete';
    delBtn.setAttribute('aria-label', 'Delete message');
    delBtn.addEventListener('click', () => {
      delBtn.disabled = true;
      onDelete(payload.id, () => {
        delBtn.disabled = false;
      });
    });
    rightMeta.appendChild(delBtn);
  }

  meta.append(name, rightMeta);

  const text = document.createElement('div');
  text.className = 'chat-message-text';
  text.textContent = payload.text;
  sender.append(meta, text);
  messages.appendChild(sender);
  messages.scrollTop = messages.scrollHeight;
}

function setupDrawerControls(resizeWhiteboard, onTabOpened, onDrawerClosed) {
  const workspace = document.getElementById('meeting-workspace');
  const drawer = document.getElementById('meeting-drawer');
  const panels = {
    participants: document.getElementById('drawer-participants-panel'),
    files: document.getElementById('drawer-files-panel'),
    chat: document.getElementById('drawer-chat-panel'),
    breakouts: document.getElementById('drawer-breakouts-panel'),
    attendance: document.getElementById('drawer-attendance-panel'),
    recordings: document.getElementById('drawer-recordings-panel'),
  };
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
    Object.entries(panels).forEach(([key, el]) => {
      el?.classList.toggle('hidden', key !== tab);
    });
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
    onTabOpened?.(tab);
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
    onDrawerClosed?.();
    drawer.classList.remove('is-open', 'is-chat-open');
    document.getElementById('toggle-chat-btn')?.classList.remove('is-active');
    document.getElementById('toggle-chat-btn')?.setAttribute('aria-expanded', 'false');
    drawer.addEventListener('transitionend', finishClosingDrawer, { once: true });
    drawerCloseTimeout = setTimeout(finishClosingDrawer, 260);
  }

  function toggleTab(tab) {
    const panel = panels[tab];
    const isOpen = drawer.classList.contains('is-open') && panel && !panel.classList.contains('hidden');
    if (isOpen) closeDrawer(); else openDrawer(tab);
  }

  document.querySelectorAll('[data-drawer-tab]').forEach((tab) => {
    tab.addEventListener('click', () => openDrawer(tab.getAttribute('data-drawer-tab')));
  });
  document.getElementById('close-drawer-btn')?.addEventListener('click', closeDrawer);
  document.getElementById('close-chat-btn')?.addEventListener('click', closeDrawer);
  document.getElementById('toggle-participants-btn')?.addEventListener('click', () => toggleTab('participants'));
  document.getElementById('toggle-files-btn')?.addEventListener('click', () => toggleTab('files'));
  document.getElementById('toggle-chat-btn')?.addEventListener('click', () => toggleTab('chat'));
  document.getElementById('toggle-breakouts-btn')?.addEventListener('click', () => toggleTab('breakouts'));

  return { openDrawer, closeDrawer };
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
  let meeting = null;
  let isTeacher = false;
  let isHost = false;
  let hasEnteredRoom = false;

  let peerManager = null;
  let fileShare = null;
  let recorder = null;
  let mediaReady = false;
  let lastMediaStateSid = null;
  let pendingRoomPeers = [];
  const pendingParticipants = new Map();
  const pendingPeerActions = [];
  const departedPeerSids = new Set();

  let currentPerms = {
    whiteboard_student_write_enabled: false,
    allow_student_screenshare: false,
    allow_student_mic: true,
    allow_student_camera: true,
    allow_student_file_share: true,
    chat_mode: 'everyone',
    is_recording: false,
  };
  let myModeration = {
    mic_locked: false,
    camera_locked: false,
    chat_muted: false,
    hand_raised: false,
    current_breakout_id: null,
  };
  let currentBreakoutId = null;
  let breakoutRooms = [];
  let breakoutCountdownTimer = null;
  const breakoutExpiryRequests = new Set();
  const recordingPreviewUrls = new Map();
  let recordingUploadInProgress = false;
  let pendingRecordingResult = null;
  let roomPageIsLeaving = false;
  let recordingDrawerActive = false;

  const user = await requireAuth();
  if (!user) return;

  const shell = document.querySelector('.meeting-shell');
  const meetingCodeFromPath = window.location.pathname.match(/^\/meeting\/([^/]+)\/?$/i)?.[1];
  const meetingCode = shell?.getAttribute('data-meeting-code') || meetingCodeFromPath?.toLowerCase();
  if (!meetingCode) {
    showToast('Invalid meeting code.', 'error');
    window.location.replace('/dashboard/');
    return;
  }

  document.getElementById('admission-leave-btn')?.addEventListener('click', () => {
    window.location.replace('/dashboard/');
  });

  function updateChatUiState() {
    const banner = document.getElementById('chat-restriction-banner');
    const input = document.getElementById('chat-message-input');
    const sendBtn = document.getElementById('chat-send-btn');
    const mode = currentPerms.chat_mode || 'everyone';
    let blockedReason = '';

    if (!isTeacher) {
      if (mode === 'disabled') blockedReason = 'Chat has been disabled by the teacher.';
      else if (mode === 'teacher_only') blockedReason = 'Chat is restricted to teacher announcements only.';
      else if (myModeration.chat_muted) blockedReason = 'Your chat permissions have been muted by the teacher.';
    } else if (mode === 'disabled') {
      blockedReason = 'Student chat is currently disabled (Teacher announcements remain active).';
    }

    if (banner) {
      banner.textContent = blockedReason;
      banner.classList.toggle('hidden', !blockedReason);
    }

    const canSend = (isTeacher || !blockedReason) && socketClient.hasJoined;
    if (input) {
      input.disabled = !isTeacher && Boolean(blockedReason);
      input.placeholder = blockedReason && !isTeacher ? blockedReason : 'Write a message…';
    }
    if (sendBtn) {
      sendBtn.disabled = !canSend;
    }
  }

  function updateRecordingBannerUi(serverIsRecording, startedAtIso = null) {
    const pill = document.getElementById('recording-status-pill');
    const label = document.getElementById('recording-status-label');
    const toggleBtn = document.getElementById('toggle-record-btn');
    const btnLabel = document.getElementById('record-btn-label');
    const startBtn = document.getElementById('panel-start-rec-btn');
    const audioStartBtn = document.getElementById('panel-start-audio-rec-btn');
    const stopBtn = document.getElementById('panel-stop-rec-btn');
    const retryBtn = document.getElementById('rec-upload-retry-btn');

    const localRec = Boolean(recorder?.isRecording);
    const active = Boolean(serverIsRecording || localRec);
    const busy = recordingUploadInProgress || Boolean(pendingRecordingResult);

    pill?.classList.toggle('hidden', !active);
    if (label) {
      label.textContent = active ? 'Recording' : 'REC';
    }
    if (toggleBtn && btnLabel) {
      toggleBtn.classList.toggle('hidden', !isTeacher);
      toggleBtn.classList.toggle('is-recording', localRec);
      toggleBtn.disabled = recordingUploadInProgress;
      btnLabel.textContent = localRec ? 'Stop Rec' : (recordingUploadInProgress ? 'Uploading…' : (pendingRecordingResult ? 'Retry Upload' : 'Record'));
    }
    if (startBtn && audioStartBtn && stopBtn) {
      startBtn.classList.toggle('hidden', busy || localRec);
      audioStartBtn.classList.toggle('hidden', busy || localRec);
      stopBtn.classList.toggle('hidden', !localRec);
    }
    if (retryBtn) {
      retryBtn.classList.toggle('hidden', !pendingRecordingResult || recordingUploadInProgress);
      retryBtn.disabled = recordingUploadInProgress;
    }
  }

  function updateBreakoutBanner() {
    const banner = document.getElementById('breakout-banner');
    const pill = document.getElementById('breakout-status-pill');
    const titleEl = document.getElementById('breakout-banner-title');
    const timerEl = document.getElementById('breakout-timer-badge');
    const returnBtn = document.getElementById('return-to-main-btn');

    if (breakoutCountdownTimer) {
      clearInterval(breakoutCountdownTimer);
      breakoutCountdownTimer = null;
    }

    const activeRoom = breakoutRooms.find((r) => String(r.id) === String(currentBreakoutId));
    if (!currentBreakoutId || !activeRoom) {
      banner?.classList.add('hidden');
      pill?.classList.add('hidden');
      return;
    }

    banner?.classList.remove('hidden');
    pill?.classList.remove('hidden');
    if (pill) pill.textContent = activeRoom.name;
    if (titleEl) titleEl.textContent = `Breakout: ${activeRoom.name}`;
    if (returnBtn) {
      const locked = !isTeacher && activeRoom.allow_return_to_main === false;
      returnBtn.disabled = locked;
      returnBtn.textContent = locked ? 'Main Return Locked' : 'Return to Main Room';
    }

    const refreshCountdown = () => {
      if (!timerEl) return;
      if (!activeRoom.ends_at) {
        timerEl.textContent = '';
        return;
      }
      const diff = Math.max(0, Math.floor((new Date(activeRoom.ends_at).getTime() - Date.now()) / 1000));
      timerEl.textContent = diff > 0 ? `Ends in ${formatElapsedSeconds(diff)}` : 'Time ending…';
      const expiryKey = `${activeRoom.id}:${activeRoom.ends_at}`;
      if (diff === 0 && socketClient.hasJoined && !breakoutExpiryRequests.has(expiryKey)) {
        breakoutExpiryRequests.add(expiryKey);
        socketClient.emit('breakout_action', { action: 'expire' });
      }
    };
    refreshCountdown();
    if (activeRoom.ends_at) {
      breakoutCountdownTimer = setInterval(refreshCountdown, 1000);
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

      const top = document.createElement('div');
      top.className = 'participant-row-top';

      const info = document.createElement('div');
      info.className = 'participant-info';
      const name = document.createElement('div');
      name.className = 'participant-name';
      name.textContent = `${p.hand_raised ? '✋ ' : ''}${p.full_name || p.username}`;
      const role = document.createElement('div');
      role.className = 'participant-role';
      const roleLabel = (p.meeting_role === 'teacher' || p.is_host) ? 'Teacher' : 'Student';
      const micTxt = p.mic_locked ? 'Mic Locked' : (p.mic_enabled === false ? 'Muted' : 'Mic On');
      const camTxt = p.screen_sharing ? 'Sharing' : (p.camera_locked ? 'Cam Locked' : (p.cam_enabled === false ? 'Cam Off' : 'Cam On'));
      const chatTxt = p.chat_muted ? ' · Chat Muted' : '';
      role.textContent = `${roleLabel} · ${micTxt} · ${camTxt}${chatTxt}`;
      info.append(name, role);
      top.appendChild(info);
      row.appendChild(top);

      if (isTeacher && !(p.meeting_role === 'teacher' || p.is_host) && Number(p.user_id) !== Number(user.id)) {
        const actions = document.createElement('div');
        actions.className = 'participant-actions';

        const addModBtn = (label, action, danger = false) => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = `mini-action-btn${danger ? ' is-danger' : ''}`;
          btn.textContent = label;
          btn.addEventListener('click', () => handleModerationAction(action, p.user_id));
          actions.appendChild(btn);
        };

        addModBtn(p.mic_locked ? 'Allow Mic' : 'Mute Mic', p.mic_locked ? 'unmute_mic' : 'mute_mic');
        addModBtn(p.camera_locked ? 'Allow Cam' : 'Disable Cam', p.camera_locked ? 'enable_cam' : 'disable_cam');
        addModBtn(p.chat_muted ? 'Unmute Chat' : 'Mute Chat', p.chat_muted ? 'unmute_chat' : 'mute_chat');
        if (p.hand_raised) {
          addModBtn('Lower Hand', 'lower_hand');
        }
        addModBtn('Remove', 'remove', true);
        row.appendChild(actions);
      }

      listEl.appendChild(row);
    });
  }

  async function renderBreakoutsPanel() {
    const listEl = document.getElementById('breakout-rooms-list');
    document.getElementById('teacher-breakout-controls')?.classList.toggle('hidden', !isTeacher);
    if (!listEl) return;

    if (!breakoutRooms.length) {
      listEl.innerHTML = '<p class="meeting-meta">No breakout rooms created yet.</p>';
      return;
    }

    let allAdmittedParticipants = [];
    if (isTeacher) {
      try {
        const detail = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/`);
        allAdmittedParticipants = (detail.participants || []).filter(
          (p) => p.admission_status === 'admitted' && p.meeting_role !== 'teacher' && !p.is_host,
        );
      } catch {
        allAdmittedParticipants = [];
      }
    }

    listEl.innerHTML = '';
    breakoutRooms.forEach((room) => {
      const card = document.createElement('div');
      card.className = 'breakout-card';

      const header = document.createElement('div');
      header.className = 'breakout-card-header';
      const titleWrap = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = room.name;
      const meta = document.createElement('div');
      meta.className = 'meeting-meta';
      meta.textContent = `${room.status.toUpperCase()} · ${(room.assigned_users || []).length} assigned`;
      titleWrap.append(title, meta);

      const btnWrap = document.createElement('div');
      const isInThisRoom = String(currentBreakoutId) === String(room.id);
      const isAssignedToMe = (room.assigned_users || []).some((u) => Number(u.user_id) === Number(user.id));

      if (room.status === 'open' && (isTeacher || isAssignedToMe || isInThisRoom)) {
        const joinBtn = document.createElement('button');
        joinBtn.type = 'button';
        joinBtn.className = `btn btn-sm ${isInThisRoom ? 'btn-secondary' : 'btn-primary'}`;
        if (isInThisRoom) {
          joinBtn.textContent = 'Leave to Main';
          joinBtn.disabled = !isTeacher && room.allow_return_to_main === false;
          joinBtn.addEventListener('click', () => handleBreakoutAction({ action: 'return_to_main' }));
        } else {
          joinBtn.textContent = isTeacher ? 'Enter Room' : 'Join Room';
          joinBtn.addEventListener('click', () => handleBreakoutAction({
            action: 'switch',
            breakout_room_id: room.id,
          }));
        }
        btnWrap.appendChild(joinBtn);
      }

      if (isTeacher) {
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn btn-danger btn-sm';
        removeBtn.textContent = 'Remove';

        const hasAssignedUsers = (room.assigned_users || []).length > 0;
        const canRemove = room.status !== 'open' && !hasAssignedUsers;

        removeBtn.disabled = !canRemove;
        removeBtn.title = !canRemove
          ? 'Close the room and unassign all participants before removing it.'
          : 'Remove this breakout room';

        removeBtn.addEventListener('click', async () => {
          if (!confirm(`Are you sure you want to remove "${room.name}"?`)) {
            return;
          }

          try {
            await apiRequest(
              `/meetings/${encodeURIComponent(meetingCode)}/breakouts/${encodeURIComponent(room.id)}/`,
              { method: 'DELETE' }
            );

            await loadBreakoutsFromRest();
            showToast('Breakout room removed.', 'success');
          } catch (err) {
            showToast(err.message || 'Could not remove breakout room.', 'error');
          }
        });

        btnWrap.appendChild(removeBtn);
      }

      header.append(titleWrap, btnWrap);
      card.appendChild(header);

      (room.assigned_users || []).forEach((u) => {
        const uRow = document.createElement('div');
        uRow.className = 'breakout-assigned-row';
        const uName = document.createElement('span');
        uName.textContent = u.full_name || u.username;
        uRow.appendChild(uName);
        if (isTeacher && room.status !== 'closed') {
          const unassignBtn = document.createElement('button');
          unassignBtn.type = 'button';
          unassignBtn.className = 'mini-action-btn';
          unassignBtn.textContent = 'Unassign';
          unassignBtn.addEventListener('click', () => handleBreakoutAction({
            action: 'assign',
            user_id: u.user_id,
            breakout_room_id: null,
          }));
          uRow.appendChild(unassignBtn);
        }
        card.appendChild(uRow);
      });

      if (isTeacher && room.status !== 'closed' && allAdmittedParticipants.length) {
        const unassignedOrOther = allAdmittedParticipants.filter(
          (p) => String(p.current_breakout_id) !== String(room.id),
        );
        if (unassignedOrOther.length) {
          const assignWrap = document.createElement('div');
          assignWrap.className = 'breakout-broadcast-row';
          const sel = document.createElement('select');
          sel.className = 'perm-select-input';
          sel.style.flex = '1';
          unassignedOrOther.forEach((st) => {
            const opt = document.createElement('option');
            opt.value = st.user_id;
            opt.textContent = st.full_name || st.username;
            sel.appendChild(opt);
          });
          const addBtn = document.createElement('button');
          addBtn.type = 'button';
          addBtn.className = 'mini-action-btn';
          addBtn.textContent = 'Assign';
          addBtn.addEventListener('click', () => handleBreakoutAction({
            action: 'assign',
            user_id: Number(sel.value),
            breakout_room_id: room.id,
          }));
          assignWrap.append(sel, addBtn);
          card.appendChild(assignWrap);
        }
      }

      listEl.appendChild(card);
    });
  }

  async function loadBreakoutsFromRest() {
    try {
      const data = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/breakouts/`);
      breakoutRooms = data.rooms || [];
      updateBreakoutBanner();
      await renderBreakoutsPanel();
    } catch {
      // Ignore if unauthorized
    }
  }

  async function loadAttendance() {
    const summaryEl = document.getElementById('attendance-summary-grid');
    const listEl = document.getElementById('attendance-records-list');
    document.getElementById('export-attendance-btn')?.classList.toggle('hidden', !isTeacher);
    if (!listEl) return;

    try {
      const report = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/attendance/`);
      const s = report.summary || {};
      if (summaryEl) {
        const boxes = [
          ['Present', s.present ?? 0],
          ['Late', s.late ?? 0],
          ['Absent', (s.absent ?? 0) + (s.rejected ?? 0) + (s.removed ?? 0)],
        ];
        summaryEl.innerHTML = boxes.map(([lbl, val]) => (
          `<div class="attendance-stat-box"><div class="attendance-stat-val tabular-nums">${val}</div><div class="attendance-stat-lbl">${lbl}</div></div>`
        )).join('');
      }

      const records = report.records || [];
      if (!records.length) {
        listEl.innerHTML = '<p class="meeting-meta">No attendance records recorded yet.</p>';
        return;
      }

      listEl.innerHTML = '';
      records.forEach((rec) => {
        const row = document.createElement('div');
        row.className = 'attendance-row';

        const top = document.createElement('div');
        top.className = 'attendance-row-top';
        const left = document.createElement('div');
        const name = document.createElement('strong');
        name.textContent = rec.full_name || rec.username;
        const meta = document.createElement('div');
        meta.className = 'meeting-meta tabular-nums';
        meta.textContent = `${rec.meeting_role} · ${rec.duration_formatted} · ${rec.join_count} join(s)${rec.is_connected ? ' · Live' : ''}`;
        left.append(name, meta);

        const pill = document.createElement('span');
        pill.className = 'attendance-status-pill';
        pill.textContent = rec.status;

        top.append(left, pill);
        row.appendChild(top);

        if (isTeacher) {
          const editRow = document.createElement('div');
          editRow.className = 'breakout-broadcast-row';
          const statusSel = document.createElement('select');
          statusSel.className = 'perm-select-input';
          [['', 'Auto'], ['present', 'Present'], ['late', 'Late'], ['excused', 'Excused'], ['absent', 'Absent']].forEach(([val, lbl]) => {
            const opt = document.createElement('option');
            opt.value = val;
            opt.textContent = lbl;
            opt.selected = (rec.status_override || '') === val;
            statusSel.appendChild(opt);
          });
          const noteInput = document.createElement('input');
          noteInput.type = 'text';
          noteInput.className = 'perm-datetime-input';
          noteInput.placeholder = 'Note…';
          noteInput.value = rec.note || '';
          const saveBtn = document.createElement('button');
          saveBtn.type = 'button';
          saveBtn.className = 'mini-action-btn';
          saveBtn.textContent = 'Save';
          saveBtn.addEventListener('click', async () => {
            saveBtn.disabled = true;
            try {
              await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/attendance/`, {
                method: 'PATCH',
                body: JSON.stringify({
                  user_id: rec.user_id,
                  status_override: statusSel.value,
                  note: noteInput.value,
                }),
              });
              showToast('Attendance updated.', 'success');
              await loadAttendance();
            } catch (err) {
              showToast(err.message || 'Could not update attendance.', 'error');
              saveBtn.disabled = false;
            }
          });
          editRow.append(statusSel, noteInput, saveBtn);
          row.appendChild(editRow);
        } else if (rec.note) {
          const noteDiv = document.createElement('div');
          noteDiv.className = 'meeting-meta';
          noteDiv.textContent = `Note: ${rec.note}`;
          row.appendChild(noteDiv);
        }

        listEl.appendChild(row);
      });
    } catch (err) {
      listEl.innerHTML = `<p class="meeting-meta">${err.message || 'Unable to load attendance.'}</p>`;
    }
  }

  async function downloadAttendanceCsv() {
    try {
      const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(meetingCode)}/attendance/export/`), {
        headers: { Authorization: `Bearer ${getAccessToken()}` },
      });
      if (!res.ok) throw new Error('Attendance export failed.');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      downloadBlobUrl(url, `attendance-${meetingCode}.csv`);
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  async function loadRecordings() {
    const listEl = document.getElementById('recordings-list');
    document.getElementById('teacher-recording-controls')?.classList.toggle('hidden', !isTeacher);
    if (!listEl) return;

    clearRecordingPreviews();
    try {
      const data = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/recordings/`);
      const recs = data.recordings || [];
      if (!recs.length) {
        listEl.innerHTML = '<p class="meeting-meta">No recordings saved for this meeting yet.</p>';
        return;
      }

      listEl.innerHTML = '';
      recs.forEach((rec) => {
        const card = document.createElement('div');
        card.className = 'recording-card';

        const hdr = document.createElement('div');
        hdr.className = 'recording-card-header';
        const info = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = rec.title;
        const meta = document.createElement('div');
        meta.className = 'meeting-meta tabular-nums';
        meta.textContent = `${formatElapsedSeconds(rec.duration_seconds)} · ${formatBytes(rec.file_size)} · ${rec.visibility === 'participants' ? 'All Participants' : 'Teacher Only'}`;
        info.append(title, meta);
        hdr.appendChild(info);
        card.appendChild(hdr);

        const previewWrap = document.createElement('div');
        const actions = document.createElement('div');
        actions.className = 'perm-bulk-actions';

        const playBtn = document.createElement('button');
        playBtn.type = 'button';
        playBtn.className = 'mini-action-btn';
        playBtn.textContent = 'Play';
        playBtn.addEventListener('click', async () => {
          playBtn.disabled = true;
          try {
            const url = await fetchAuthorizedRecordingBlobUrl(rec.id, true);
            if (roomPageIsLeaving || !recordingDrawerActive || !previewWrap.isConnected) {
              URL.revokeObjectURL(url);
              return;
            }
            const previousUrl = recordingPreviewUrls.get(rec.id);
            const previousPlayer = previewWrap.querySelector('video,audio');
            releaseRecordingPreview(previousPlayer, previousUrl);
            recordingPreviewUrls.delete(rec.id);
            recordingPreviewUrls.set(rec.id, url);
            const player = createRecordingPlayer(rec.mime_type, 'recording-video-preview');
            player.dataset.recordingId = rec.id;
            player.addEventListener('error', () => {
              const fallback = document.createElement('p');
              fallback.className = 'meeting-meta';
              fallback.textContent = 'This browser cannot preview this recording format. Download it and open it in a compatible media player.';
              player.replaceWith(fallback);
            }, { once: true });
            player.src = url;
            previewWrap.innerHTML = '';
            previewWrap.appendChild(player);
            playBtn.disabled = false;
          } catch (err) {
            showToast(err.message, 'error');
            playBtn.disabled = false;
          }
        });

        const dlBtn = document.createElement('button');
        dlBtn.type = 'button';
        dlBtn.className = 'mini-action-btn';
        dlBtn.textContent = 'Download';
        dlBtn.addEventListener('click', async () => {
          try {
            const url = await fetchAuthorizedRecordingBlobUrl(rec.id, false);
            downloadBlobUrl(url, rec.original_name || `${rec.title}.webm`);
          } catch (err) {
            showToast(err.message, 'error');
          }
        });

        actions.append(playBtn, dlBtn);

        if (isTeacher) {
          const visBtn = document.createElement('button');
          visBtn.type = 'button';
          visBtn.className = 'mini-action-btn';
          const nextVis = rec.visibility === 'participants' ? 'teacher_only' : 'participants';
          visBtn.textContent = nextVis === 'participants' ? 'Share w/ Class' : 'Make Private';
          visBtn.addEventListener('click', async () => {
            try {
              await apiRequest(`/meetings/recordings/${rec.id}/`, {
                method: 'PATCH',
                body: JSON.stringify({ visibility: nextVis }),
              });
              showToast('Recording visibility updated.', 'success');
              await loadRecordings();
            } catch (err) {
              showToast(err.message, 'error');
            }
          });

          const delBtn = document.createElement('button');
          delBtn.type = 'button';
          delBtn.className = 'mini-action-btn is-danger';
          delBtn.textContent = 'Delete';
          delBtn.addEventListener('click', async () => {
            try {
              await apiRequest(`/meetings/recordings/${rec.id}/`, { method: 'DELETE' });
              showToast('Recording deleted.', 'info');
              await loadRecordings();
            } catch (err) {
              showToast(err.message, 'error');
            }
          });
          actions.append(visBtn, delBtn);
        }

        card.append(actions, previewWrap);
        listEl.appendChild(card);
      });
    } catch (err) {
      listEl.innerHTML = `<p class="meeting-meta">${err.message || 'Unable to load recordings.'}</p>`;
    }
  }

  function clearRecordingPreviews() {
    const listEl = document.getElementById('recordings-list');
    const liveUrls = new Set();
    listEl?.querySelectorAll('video,audio').forEach((player) => {
      const url = recordingPreviewUrls.get(player.dataset.recordingId);
      if (url) liveUrls.add(url);
      releaseRecordingPreview(player, url);
    });
    for (const url of recordingPreviewUrls.values()) {
      if (!liveUrls.has(url)) URL.revokeObjectURL(url);
    }
    recordingPreviewUrls.clear();
  }

  const drawerCtrl = setupDrawerControls(
    () => whiteboard?.resizeCanvas(),
    (tab) => {
      recordingDrawerActive = tab === 'recordings';
      if (!recordingDrawerActive) clearRecordingPreviews();
      if (tab === 'breakouts') renderBreakoutsPanel();
      if (tab === 'attendance') loadAttendance();
      if (tab === 'recordings') loadRecordings();
    },
    () => {
      recordingDrawerActive = false;
      clearRecordingPreviews();
    },
  );

  function runPeerAction(action, sid) {
    if (sid && departedPeerSids.has(sid)) return;
    const guardedAction = () => {
      if (sid && departedPeerSids.has(sid)) return;
      return action();
    };
    if (mediaReady) return guardedAction();
    pendingPeerActions.push(guardedAction);
  }

  function applyRoleAndPermissions(perms = {}, roleOverride = null) {
    if (roleOverride) {
      isTeacher = roleOverride === 'teacher';
    }
    currentPerms = { ...currentPerms, ...perms };
    const roleBadge = document.getElementById('room-role-badge');
    roleBadge?.classList.add('hidden');

    document.getElementById('end-meeting-btn')?.classList.toggle('hidden', !isTeacher);
    document.getElementById('teacher-controls-panel')?.classList.toggle('hidden', !isTeacher);
    document.getElementById('teacher-breakout-controls')?.classList.toggle('hidden', !isTeacher);
    document.getElementById('teacher-recording-controls')?.classList.toggle('hidden', !isTeacher);

    const wbStudentWrite = Boolean(currentPerms.whiteboard_student_write_enabled);
    const allowStudentShare = Boolean(currentPerms.allow_student_screenshare);
    const allowStudentMic = currentPerms.allow_student_mic !== false;
    const allowStudentCam = currentPerms.allow_student_camera !== false;
    const allowStudentFile = currentPerms.allow_student_file_share !== false;
    const chatMode = currentPerms.chat_mode || 'everyone';

    const inOpenAssignedBreakout = Boolean(
      currentBreakoutId
      && breakoutRooms.some((r) => String(r.id) === String(currentBreakoutId) && r.status === 'open'),
    );
    const canEditWb = isTeacher || wbStudentWrite || inOpenAssignedBreakout;
    const canClearWb = isTeacher;

    whiteboard?.setPermissions({ canEdit: canEditWb, canClear: canClearWb });
    fileShare?.setAllowStudentUpload(allowStudentFile, isTeacher);
    peerManager?.setPermissions({
      isTeacher,
      allowStudentScreenshare: allowStudentShare,
      allowStudentMic,
      allowStudentCamera: allowStudentCam,
      cameraLocked: myModeration.camera_locked,
      activeScreenSharer: {
        id: currentPerms.active_screen_sharer_id ?? null,
        username: currentPerms.active_screen_sharer_username ?? null,
        sid: currentPerms.active_screen_sharer_sid ?? '',
      },
    });

    const wbToggle = document.getElementById('perm-wb-student-toggle');
    if (wbToggle) wbToggle.checked = wbStudentWrite;
    const scrToggle = document.getElementById('perm-student-screenshare-toggle');
    if (scrToggle) scrToggle.checked = allowStudentShare;
    const micToggle = document.getElementById('perm-mic-toggle');
    if (micToggle) micToggle.checked = allowStudentMic;
    const camToggle = document.getElementById('perm-cam-toggle');
    if (camToggle) camToggle.checked = allowStudentCam;
    const fileToggle = document.getElementById('perm-fileshare-toggle');
    if (fileToggle) fileToggle.checked = allowStudentFile;
    const chatSelect = document.getElementById('perm-chat-mode-select');
    if (chatSelect) chatSelect.value = chatMode;
    const deadlineInput = document.getElementById('perm-admission-deadline-input');
    if (deadlineInput && Object.prototype.hasOwnProperty.call(perms, 'admission_deadline')) {
      deadlineInput.value = toLocalDateTimeValue(perms.admission_deadline);
    }

    updateChatUiState();
    updateTeacherStopShareBtn(currentPerms);
    updateRecordingBannerUi(currentPerms.is_recording, currentPerms.recording_started_at);
  }

  function syncInitialMediaState() {
    if (!mediaReady || !peerManager || !socketClient.hasJoined || !peerManager.localSid) return;
    if (lastMediaStateSid === peerManager.localSid) return;
    peerManager.broadcastMediaState();
    lastMediaStateSid = peerManager.localSid;
  }

  function updateTeacherStopShareBtn(state = {}) {
    const stopBtn = document.getElementById('teacher-stop-screenshare-btn');
    if (!stopBtn) return;
    const sharerId = state.active_screen_sharer_id;
    const sharerSid = state.active_screen_sharer_sid;
    const show = isTeacher && Boolean(sharerId || sharerSid) && Number(sharerId) !== Number(user.id);
    stopBtn.classList.toggle('hidden', !show);
  }

  async function sendPermissionUpdate(patch) {
    if (socketClient.emit('update_meeting_permissions', patch)) {
      return;
    }
    try {
      const updated = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/permissions/`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      applyRoleAndPermissions(updated, updated.meeting_role);
    } catch (err) {
      showToast(err.message || 'Unable to update permissions.', 'error');
    }
  }

  async function handleModerationAction(action, targetUserId = null) {
    const payload = targetUserId !== null ? { action, user_id: targetUserId } : { action };
    if (socketClient.emit('moderate_participant', payload)) {
      return;
    }
    try {
      await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/moderation/`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      showToast('Moderation applied.', 'success');
    } catch (err) {
      showToast(err.message || 'Moderation failed.', 'error');
    }
  }

  async function handleBreakoutAction(payload) {
    if (socketClient.emit('breakout_action', payload)) {
      return;
    }
    try {
      if (payload.action === 'create') {
        const res = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/breakouts/`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        breakoutRooms = res.rooms || [];
      } else {
        const res = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/breakouts/action/`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        if (res.rooms) breakoutRooms = res.rooms;
      }
      updateBreakoutBanner();
      await renderBreakoutsPanel();
    } catch (err) {
      showToast(err.message || 'Breakout action failed.', 'error');
    }
  }

  async function handleDeleteChatMessage(messageId, onErrorReset) {
    if (socketClient.emit('delete_chat_message', { message_id: messageId })) {
      return;
    }
    try {
      await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/chat/${encodeURIComponent(messageId)}/`, {
        method: 'DELETE',
      });
      removeChatMessageElement(messageId);
    } catch (err) {
      onErrorReset?.();
      showToast(err.message || 'Unable to delete message.', 'error');
    }
  }

  async function handleSubchannelSwap(payload, toastMessage) {
    currentBreakoutId = payload.breakout_room_id || null;
    if (Array.isArray(payload.breakout_rooms)) {
      breakoutRooms = payload.breakout_rooms;
    }
    if (payload.permissions) {
      applyRoleAndPermissions(payload.permissions);
    } else {
      applyRoleAndPermissions({});
    }
    updateBreakoutBanner();
    await renderBreakoutsPanel();

    if (whiteboard && Array.isArray(payload.whiteboard_ops)) {
      whiteboard.loadState(payload.whiteboard_ops);
    }
    const msgBox = document.getElementById('chat-messages');
    if (msgBox && Array.isArray(payload.chat_messages)) {
      msgBox.innerHTML = '';
      payload.chat_messages.forEach((m) => renderChatMessage(m, user, {
        isTeacher,
        onDelete: handleDeleteChatMessage,
      }));
    }

    if (peerManager) {
      peerManager.resetRemotePeers();
      departedPeerSids.clear();
      pendingParticipants.clear();
      for (const p of payload.peers || []) {
        departedPeerSids.delete(p.sid);
      }
      if (mediaReady) {
        await peerManager.syncRoomPeers(payload.peers || []);
      } else {
        pendingRoomPeers = payload.peers || [];
      }
      recorder?.refreshAudioSources(peerManager);
    }

    if (toastMessage) {
      showToast(toastMessage, 'info');
    }
  }

  async function startLocalAndServerRecording(audioOnly = false) {
    if (!isTeacher || !recorder || recorder.isRecording || recordingUploadInProgress) return;
    if (pendingRecordingResult) {
      showToast('Save or retry the pending recording before starting another.', 'warning');
      return;
    }
    try {
      await recorder.start(peerManager, {
        audioOnly,
        onAutoStop: () => stopAndUploadRecording(),
      });
      socketClient.emit('start_recording', {});
      updateRecordingBannerUi(true);
      showToast(audioOnly ? 'Audio recording started.' : 'Session recording started.', 'success');
    } catch (err) {
      updateRecordingBannerUi(currentPerms.is_recording);
      if (err?.name === 'NotAllowedError' || err?.name === 'AbortError') {
        showToast('Screen capture cancelled. You can use Audio-Only Rec or pick a tab.', 'warning');
      } else {
        showToast(err.message || 'Unable to start recording.', 'error');
      }
    }
  }

  async function uploadPendingRecording() {
    if (!pendingRecordingResult || recordingUploadInProgress) return;
    recordingUploadInProgress = true;
    const progressTrack = document.getElementById('rec-upload-progress-track');
    const progressFill = document.getElementById('rec-upload-progress-fill');
    const statusNote = document.getElementById('rec-status-note');
    const pending = pendingRecordingResult;
    updateRecordingBannerUi(currentPerms.is_recording);

    try {
      progressTrack?.classList.remove('hidden');
      if (progressFill) progressFill.style.width = '0%';
      if (statusNote) statusNote.textContent = 'Uploading recording…';

      await uploadSessionRecording({
        meetingCode,
        blob: pending.blob,
        extension: pending.extension,
        uploadId: pending.uploadId,
        title: pending.title,
        visibility: pending.visibility,
        durationSeconds: pending.durationSeconds,
        onProgress: (pct) => {
          if (progressFill) progressFill.style.width = `${pct}%`;
          if (statusNote) statusNote.textContent = `Uploading recording (${pct}%)`;
        },
      });
      pendingRecordingResult = null;
      if (statusNote) statusNote.textContent = 'Recording saved and ready.';
      showToast('Session recording saved.', 'success');
      await loadRecordings();
    } catch (err) {
      showToast(err.message || 'Failed to upload recording.', 'error');
      if (statusNote) {
        statusNote.textContent = 'Upload did not complete. The recording is retained in memory; keep this page open and retry.';
      }
    } finally {
      recordingUploadInProgress = false;
      updateRecordingBannerUi(currentPerms.is_recording);
      setTimeout(() => {
        progressTrack?.classList.add('hidden');
        if (progressFill) progressFill.style.width = '0%';
      }, 1200);
    }
  }

  async function stopAndUploadRecording() {
    if (!recorder || !recorder.isRecording || recordingUploadInProgress) return;
    recordingUploadInProgress = true;
    updateRecordingBannerUi(currentPerms.is_recording);

    let result;
    try {
      socketClient.emit('stop_recording', {});
      result = await recorder.stop();
    } catch (err) {
      recordingUploadInProgress = false;
      updateRecordingBannerUi(currentPerms.is_recording);
      const statusNote = document.getElementById('rec-status-note');
      const message = err.message || 'Recording could not be finalized; no upload was attempted.';
      if (statusNote) statusNote.textContent = message;
      showToast(message, 'error');
      return;
    }

    updateRecordingBannerUi(false);
    if (!result?.blob || result.blob.size === 0) {
      recordingUploadInProgress = false;
      updateRecordingBannerUi(currentPerms.is_recording);
      showToast('Recording was empty; nothing saved.', 'warning');
      return;
    }

    const cryptoApi = globalThis.crypto;
    let uploadId = cryptoApi?.randomUUID?.();
    if (!uploadId && cryptoApi?.getRandomValues) {
      const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
      uploadId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    if (!uploadId) {
      recordingUploadInProgress = false;
      updateRecordingBannerUi(currentPerms.is_recording);
      showToast('Secure recording upload identifiers are unavailable in this browser; the recording was not uploaded.', 'error');
      return;
    }
    pendingRecordingResult = {
      ...result,
      uploadId,
      title: `${meeting?.title || 'Session'} Recording`,
      visibility: document.getElementById('rec-visibility-select')?.value || 'teacher_only',
    };
    recordingUploadInProgress = false;
    updateRecordingBannerUi(currentPerms.is_recording);
    await uploadPendingRecording();
  }

  const socketClient = new MeetingSocketClient(meetingCode, {
    onConnectionState: (state) => {
      setSocketStatus(state, socketClient.hasJoined);
      whiteboard?.setEnabled(state === 'connected' && socketClient.hasJoined);
      updateChatUiState();
    },
    onError: (msg) => {
      if (!socketClient.hasJoined) {
        setSocketStatus('disconnected');
        whiteboard?.setEnabled(false);
      }
      showToast(msg, 'error');
    },
    onParticipantRemoved: (payload) => {
      socketClient.disconnect();
      peerManager?.cleanup();
      showAdmissionGate({
        title: 'Removed From Meeting',
        message: payload?.message || 'You were removed from this meeting by the teacher.',
        isError: true,
      });
    },

    onRoomJoined: async (payload) => {
      isHost = Boolean(payload.is_host);
      setSocketStatus('connected', true, false);
      currentBreakoutId = payload.breakout_room_id || null;
      if (Array.isArray(payload.breakout_rooms)) {
        breakoutRooms = payload.breakout_rooms;
      }
      if (payload.my_moderation) {
        myModeration = { ...myModeration, ...payload.my_moderation };
        peerManager?.applyModeration(myModeration);
      }
      applyRoleAndPermissions(payload.permissions || {}, payload.meeting_role);
      updateBreakoutBanner();
      await renderBreakoutsPanel();
      if (whiteboard && Array.isArray(payload.whiteboard_ops)) {
        whiteboard.loadState(payload.whiteboard_ops);
        whiteboard.setEnabled(true);
      }
      if (Array.isArray(payload.chat_messages)) {
        const msgBox = document.getElementById('chat-messages');
        if (msgBox) msgBox.innerHTML = '';
        payload.chat_messages.forEach((m) => renderChatMessage(m, user, {
          isTeacher,
          onDelete: handleDeleteChatMessage,
        }));
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
      syncInitialMediaState();
    },
    onParticipantJoined: async (payload) => {
      if (peerManager && payload?.participant) {
        departedPeerSids.delete(payload.participant.sid);
        if (!mediaReady) {
          pendingParticipants.set(payload.participant.sid, payload.participant);
          return;
        }
        await peerManager.handleParticipantJoined(payload.participant);
        recorder?.refreshAudioSources(peerManager);
        if (payload.participant.username !== user.username) {
          showToast(`${payload.participant.full_name || payload.participant.username} joined.`, 'info');
        }
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
      () => {
        peerManager?.handleRemoteMediaState(payload);
        recorder?.refreshAudioSources(peerManager);
      },
      payload?.sid,
    ),
    onParticipantHandState: (payload) => {
      if (!payload) return;
      if (Number(payload.user_id) === Number(user.id)) {
        myModeration.hand_raised = Boolean(payload.hand_raised);
      }
      peerManager?.updateHandState(payload);
      if (isTeacher && payload.hand_raised && Number(payload.user_id) !== Number(user.id)) {
        showToast(`${payload.username} raised their hand.`, 'info', 4000);
      }
    },
    onModerationEnforced: (payload) => {
      if (!payload) return;
      if (typeof payload.mic_locked === 'boolean') myModeration.mic_locked = payload.mic_locked;
      if (typeof payload.camera_locked === 'boolean') myModeration.camera_locked = payload.camera_locked;
      if (typeof payload.chat_muted === 'boolean') myModeration.chat_muted = payload.chat_muted;
      if (typeof payload.hand_raised === 'boolean') myModeration.hand_raised = payload.hand_raised;
      peerManager?.applyModeration(payload);
      updateChatUiState();
      if (payload.action === 'mute_mic' || payload.action === 'mute_all_mics') {
        showToast('Your microphone was muted by the teacher.', 'warning');
      } else if (payload.action === 'disable_cam' || payload.action === 'disable_all_cams') {
        showToast('Your camera was disabled by the teacher.', 'warning');
      } else if (payload.action === 'mute_chat') {
        showToast('Your chat was muted by the teacher.', 'warning');
      }
    },
    onPermissionsUpdated: (payload) => {
      if (payload?.permissions) {
        const previousCameraPermission = currentPerms.allow_student_camera !== false;
        applyRoleAndPermissions(payload.permissions);
        const currentCameraPermission = currentPerms.allow_student_camera !== false;
        if (previousCameraPermission !== currentCameraPermission) {
          showToast(
            currentCameraPermission
              ? 'The teacher enabled student cameras. You can turn on your camera when ready.'
              : 'The teacher disabled student cameras. Your camera is off.',
            currentCameraPermission ? 'info' : 'warning',
          );
        } else {
          showToast('Classroom permissions updated by the teacher.', 'info');
        }
      }
    },
    onBreakoutStateUpdated: async (payload) => {
      if (Array.isArray(payload?.breakout_rooms)) {
        breakoutRooms = payload.breakout_rooms;
        updateBreakoutBanner();
        await renderBreakoutsPanel();
        applyRoleAndPermissions({});
      }
    },
    onBreakoutEntered: (payload) => handleSubchannelSwap(
      payload,
      `Entered ${payload.breakout_room?.name || 'Breakout Room'}.`,
    ),
    onBreakoutExited: (payload) => handleSubchannelSwap(payload, 'Returned to the Main Room.'),
    onBreakoutAnnouncement: (payload) => {
      if (payload?.message) {
        showToast(`Announcement (${payload.sender || 'Teacher'}): ${payload.message}`, 'info', 9000);
      }
    },
    onRecordingStateChanged: (payload) => {
      currentPerms.is_recording = Boolean(payload?.is_recording);
      updateRecordingBannerUi(currentPerms.is_recording, payload?.started_at);
      if (payload?.is_recording) {
        showToast('Session recording started by the teacher.', 'info');
      } else {
        showToast('Session recording stopped.', 'info');
        loadRecordings();
      }
    },
    onAttendanceUpdated: () => {
      const panel = document.getElementById('drawer-attendance-panel');
      if (panel && !panel.classList.contains('hidden')) {
        loadAttendance();
      }
    },
    onScreenShareGranted: (payload) => peerManager?.handleScreenShareGranted(payload),
    onScreenShareDenied: (payload) => peerManager?.handleScreenShareDenied(payload),
    onScreenShareRevoked: (payload) => peerManager?.handleScreenShareRevoked(payload),
    onScreenShareState: (payload) => {
      peerManager?.handleScreenShareState(payload);
      updateTeacherStopShareBtn(payload);
    },
    onMeetingEnded: () => {
      showToast('This meeting was ended by the teacher.', 'warning');
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
    onChatMessage: (payload) => renderChatMessage(payload, user, {
      isTeacher,
      onDelete: handleDeleteChatMessage,
    }),
    onChatMessageDeleted: (payload) => removeChatMessageElement(payload?.message_id),
    onFileShared: (payload) => fileShare?.addRemoteFile(payload?.file),
  });

  function setupChatForm() {
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

  async function enterAdmittedRoom(admittedMeeting) {
    if (hasEnteredRoom) return;
    hasEnteredRoom = true;
    meeting = admittedMeeting;
    isTeacher = Boolean(meeting.is_host || meeting.meeting_role === 'teacher');
    if (meeting.my_moderation) {
      myModeration = { ...myModeration, ...meeting.my_moderation };
      currentBreakoutId = meeting.my_moderation.current_breakout_id || null;
    }

    document.getElementById('room-meeting-title').textContent = meeting.title;
    document.getElementById('room-meeting-code').textContent = meeting.meeting_code;

    let iceServers = [{ urls: 'stun:stun.l.google.com:19302' }];
    try {
      const iceRes = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/ice-servers/`);
      if (Array.isArray(iceRes.ice_servers) && iceRes.ice_servers.length) {
        iceServers = iceRes.ice_servers;
      }
    } catch {
      // Fallback to public STUN
    }

    peerManager = new PeerConnectionManager({
      socketClient,
      iceServers,
      localUser: user,
      onParticipantsChanged: renderParticipantsList,
      onTrackAdded: () => {
        recorder?.refreshAudioSources(peerManager);
      },
      onTrackRemoved: () => {
        recorder?.refreshAudioSources(peerManager);
      },
      onPeerRemoved: (peerEntry) => {
        recorder?.removeAudioSourcesForPeer(peerEntry);
      },
      refreshCameraPermissions: async () => {
        const latest = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/`, {
          cache: 'no-store',
        });
        isTeacher = Boolean(latest.is_host || latest.meeting_role === 'teacher');
        if (latest.my_moderation) {
          myModeration = { ...myModeration, ...latest.my_moderation };
        }
        applyRoleAndPermissions(latest, latest.meeting_role);
        peerManager?.applyModeration(myModeration);
        return {
          isTeacher,
          allowStudentCamera: latest.allow_student_camera !== false,
          cameraLocked: Boolean(latest.my_moderation?.camera_locked),
        };
      },
    });

    whiteboard = new CollaborativeWhiteboard({
      canvasId: 'whiteboard-canvas',
      wrapId: 'wb-canvas-wrap',
      socketClient,
    });

    fileShare = new MeetingFileShare(meetingCode, socketClient);
    recorder = new MeetingSessionRecorder({
      onStateChange: () => updateRecordingBannerUi(currentPerms.is_recording),
      onTick: (secs) => {
        const el = document.getElementById('rec-elapsed-label');
        if (el) el.textContent = formatElapsedSeconds(secs);
      },
      onError: (err) => {
        if (currentPerms.is_recording) {
          currentPerms.is_recording = false;
          socketClient.emit('stop_recording', {});
          updateRecordingBannerUi(false);
        }
        showToast(err.message || 'Recording error.', 'error');
      },
    });

    const setStageView = setupStageViewControls(whiteboard);
    setupChatForm();
    applyRoleAndPermissions(meeting, meeting.meeting_role);

    // Teacher Moderation & Permissions Controls
    document.getElementById('perm-wb-student-toggle')?.addEventListener('change', (e) => {
      sendPermissionUpdate({ whiteboard_student_write_enabled: Boolean(e.target.checked) });
    });
    document.getElementById('perm-student-screenshare-toggle')?.addEventListener('change', (e) => {
      sendPermissionUpdate({ allow_student_screenshare: Boolean(e.target.checked) });
    });
    document.getElementById('perm-mic-toggle')?.addEventListener('change', (e) => {
      sendPermissionUpdate({ allow_student_mic: Boolean(e.target.checked) });
    });
    document.getElementById('perm-cam-toggle')?.addEventListener('change', (e) => {
      sendPermissionUpdate({ allow_student_camera: Boolean(e.target.checked) });
    });
    document.getElementById('perm-fileshare-toggle')?.addEventListener('change', (e) => {
      sendPermissionUpdate({ allow_student_file_share: Boolean(e.target.checked) });
    });
    document.getElementById('perm-chat-mode-select')?.addEventListener('change', (e) => {
      sendPermissionUpdate({ chat_mode: e.target.value });
    });
    document.getElementById('btn-mute-all-mics')?.addEventListener('click', () => {
      handleModerationAction('mute_all_mics');
    });
    document.getElementById('btn-disable-all-cams')?.addEventListener('click', () => {
      handleModerationAction('disable_all_cams');
    });
    document.getElementById('teacher-stop-screenshare-btn')?.addEventListener('click', () => {
      socketClient.emit('stop_screen_share', {});
    });
    document.getElementById('perm-save-deadline-btn')?.addEventListener('click', () => {
      const val = document.getElementById('perm-admission-deadline-input')?.value;
      if (!val) {
        showToast('Select a date and time for the admission deadline.', 'warning');
        return;
      }
      sendPermissionUpdate({ admission_deadline: new Date(val).toISOString() });
    });
    document.getElementById('perm-clear-deadline-btn')?.addEventListener('click', () => {
      sendPermissionUpdate({ admission_deadline: null });
    });

    // Breakout Controls
    document.getElementById('breakout-create-btn')?.addEventListener('click', () => {
      const count = Number(document.getElementById('breakout-count-input')?.value || 2);
      const durVal = document.getElementById('breakout-duration-input')?.value;
      const allowReturn = Boolean(document.getElementById('breakout-allow-return-toggle')?.checked);
      handleBreakoutAction({
        action: 'create',
        count,
        duration_minutes: durVal ? Number(durVal) : null,
        allow_return_to_main: allowReturn,
      });
    });
    document.getElementById('breakout-auto-assign-btn')?.addEventListener('click', () => {
      handleBreakoutAction({ action: 'auto_assign' });
    });
    document.getElementById('breakout-open-all-btn')?.addEventListener('click', () => {
      const durVal = document.getElementById('breakout-duration-input')?.value;
      handleBreakoutAction({
        action: 'open_all',
        duration_minutes: durVal ? Number(durVal) : null,
      });
    });
    document.getElementById('breakout-close-all-btn')?.addEventListener('click', () => {
      handleBreakoutAction({ action: 'close_all' });
    });
    document.getElementById('breakout-broadcast-btn')?.addEventListener('click', () => {
      const input = document.getElementById('breakout-broadcast-input');
      const msg = input?.value?.trim();
      if (!msg) return;
      handleBreakoutAction({ action: 'broadcast', message: msg });
      if (input) input.value = '';
    });
    document.getElementById('return-to-main-btn')?.addEventListener('click', () => {
      handleBreakoutAction({ action: 'return_to_main' });
    });

    // Attendance Controls
    document.getElementById('refresh-attendance-btn')?.addEventListener('click', loadAttendance);
    document.getElementById('export-attendance-btn')?.addEventListener('click', downloadAttendanceCsv);

    // Recording Controls
    document.getElementById('toggle-record-btn')?.addEventListener('click', () => {
      if (!isTeacher) return;
      if (recorder.isRecording) stopAndUploadRecording();
      else if (pendingRecordingResult) uploadPendingRecording();
      else drawerCtrl.openDrawer('recordings');
    });
    document.getElementById('panel-start-rec-btn')?.addEventListener('click', () => startLocalAndServerRecording(false));
    document.getElementById('panel-start-audio-rec-btn')?.addEventListener('click', () => startLocalAndServerRecording(true));
    document.getElementById('panel-stop-rec-btn')?.addEventListener('click', () => stopAndUploadRecording());
    document.getElementById('rec-upload-retry-btn')?.addEventListener('click', () => uploadPendingRecording());

    // Media & Raise Hand Controls
    document.getElementById('toggle-mic-btn')?.addEventListener('click', () => peerManager.toggleMic());
    document.getElementById('toggle-cam-btn')?.addEventListener('click', () => peerManager.toggleCamera());
    document.getElementById('toggle-screen-btn')?.addEventListener('click', () => peerManager.toggleScreenShare());
    document.getElementById('toggle-hand-btn')?.addEventListener('click', () => {
      socketClient.emit('raise_hand', { raised: !peerManager.handRaised });
    });

    document.getElementById('copy-invite-btn')?.addEventListener('click', async () => {
      const link = `${window.location.origin}/meeting/${meetingCode}/`;
      await copyToClipboard(link);
      showToast('Meeting invitation link copied.', 'success');
    });

    document.getElementById('leave-meeting-btn')?.addEventListener('click', async () => {
      if (recorder?.isRecording) {
        await stopAndUploadRecording();
      }
      if (pendingRecordingResult) {
        showToast('The recording is still waiting to upload. Retry it before leaving, or keep this page open.', 'error');
        return;
      }
      try {
        await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/leave/`, { method: 'POST' });
      } catch {
        // Proceed with cleanup
      }
      socketClient.disconnect();
      peerManager.cleanup();
      window.location.replace('/dashboard/');
    });

    const endBtn = document.getElementById('end-meeting-btn');
    endBtn?.addEventListener('click', async () => {
      endBtn.disabled = true;
      if (recorder?.isRecording) {
        await stopAndUploadRecording();
      }
      if (pendingRecordingResult) {
        showToast('The recording is still waiting to upload. Retry it before ending the meeting.', 'error');
        endBtn.disabled = false;
        return;
      }
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

    // Deep-linked query params (?view=whiteboard or ?drawer=files/chat/participants/breakouts/attendance/recordings)
    const params = new URLSearchParams(window.location.search);
    if (params.get('view') === 'whiteboard') setStageView('whiteboard');
    const drawerParam = params.get('drawer');
    if (['files', 'chat', 'participants', 'breakouts', 'attendance', 'recordings'].includes(drawerParam)) {
      drawerCtrl.openDrawer(drawerParam);
    }

    const mediaPromise = peerManager.initLocalMedia();
    try {
      socketClient.upgradeToRoom();
    } catch (err) {
      setSocketStatus('disconnected');
      showToast(err.message || 'Real-time connection could not be started.', 'error');
    }
    const filesPromise = fileShare.loadFiles();
    const breakoutsPromise = loadBreakoutsFromRest();

    await mediaPromise;
    mediaReady = true;
    peerManager.applyModeration(myModeration);
    await peerManager.syncRoomPeers(pendingRoomPeers);
    pendingRoomPeers = [];
    for (const participant of pendingParticipants.values()) {
      await peerManager.handleParticipantJoined(participant);
    }
    pendingParticipants.clear();
    while (pendingPeerActions.length) {
      await pendingPeerActions.shift()();
    }
    syncInitialMediaState();
    renderParticipantsList(peerManager.getParticipantList());
    await filesPromise;
    await breakoutsPromise;

    window.addEventListener('beforeunload', (event) => {
      if (recorder?.isRecording || recordingUploadInProgress || pendingRecordingResult) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
    window.addEventListener('pagehide', () => {
      roomPageIsLeaving = true;
      clearRecordingPreviews();
      socketClient.disconnect();
      peerManager?.cleanup();
    });
  }

  // Initial admission check
  try {
    meeting = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/join/`, {
      method: 'POST',
    });
  } catch (err) {
    let title = 'Unable to Join Meeting';
    if (err.data?.code === 'admission_expired') title = 'Meeting Admission Window Closed';
    else if (err.data?.code === 'not_started') title = 'Meeting Not Started Yet';
    else if (err.data?.code === 'admission_removed') title = 'Removed From Meeting';

    showAdmissionGate({
      title,
      message: err.message || 'Unable to join meeting.',
      isError: true,
    });
    return;
  }

  document.getElementById('room-meeting-title').textContent = meeting.title || 'CONVIA Meeting';
  document.getElementById('room-meeting-code').textContent = meeting.meeting_code || meetingCode;

  hideAdmissionGate();
  await enterAdmittedRoom(meeting);
}

document.addEventListener('DOMContentLoaded', initMeetingRoom);
