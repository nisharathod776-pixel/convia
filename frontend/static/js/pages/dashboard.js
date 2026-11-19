import { apiRequest, apiUrl, clearAuthStorage, getAccessToken } from '../core/api.js';
import { extractMeetingCode, requireAuth } from '../core/auth.js';
import { initMeetingRecordsModal } from '../core/meeting-records-modal.js';
import { copyToClipboard, downloadBlobUrl, formatBytes, formatDateTime, setFieldError, showToast } from '../core/ui.js';

let allMeetings = [];
let activeFilter = 'all';
let dashboardLoadPromise = null;
let recordsModal = null;

function renderMeetingsTable() {
  const container = document.getElementById('meetings-table-container');
  if (!container) return;

  const filtered = allMeetings.filter((meeting) => {
    if (activeFilter === 'all') return true;
    if (activeFilter === 'hosted') return meeting.is_host;
    return meeting.status === activeFilter;
  });

  if (!filtered.length) {
    container.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    const p = document.createElement('p');
    p.textContent = activeFilter === 'all'
      ? 'No meetings yet. Create your first meeting room to start collaborating.'
      : `No ${activeFilter} meetings found.`;
    empty.appendChild(p);
    container.appendChild(empty);
    return;
  }

  const table = document.createElement('table');
  table.className = 'data-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>Title</th>
        <th>Meeting ID</th>
        <th>Status</th>
        <th>Host</th>
        <th class="num">Participants</th>
        <th>Created</th>
        <th class="num">Actions</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;

  const tbody = table.querySelector('tbody');
  filtered.forEach((meeting) => {
    const tr = document.createElement('tr');

    const titleTd = document.createElement('td');
    const titleStrong = document.createElement('strong');
    titleStrong.textContent = meeting.title;
    titleTd.appendChild(titleStrong);
    if (meeting.scheduled_at) {
      const sub = document.createElement('div');
      sub.className = 'field-hint';
      sub.textContent = `Scheduled: ${formatDateTime(meeting.scheduled_at)}`;
      titleTd.appendChild(sub);
    }
    if (meeting.admission_deadline) {
      const dlSub = document.createElement('div');
      dlSub.className = 'field-hint';
      dlSub.textContent = `Admission closes: ${formatDateTime(meeting.admission_deadline)}`;
      titleTd.appendChild(dlSub);
    }

    const codeTd = document.createElement('td');
    codeTd.className = 'mono';
    codeTd.textContent = meeting.meeting_code;

    const statusTd = document.createElement('td');
    const statusSpan = document.createElement('span');
    statusSpan.className = `status-text is-${meeting.status}`;
    statusSpan.textContent = meeting.status.charAt(0).toUpperCase() + meeting.status.slice(1);
    statusTd.appendChild(statusSpan);

    const hostTd = document.createElement('td');
    hostTd.textContent = meeting.host_username || '—';

    const countTd = document.createElement('td');
    countTd.className = 'num tabular-nums';
    countTd.textContent = String(meeting.participant_count ?? 0);

    const createdTd = document.createElement('td');
    createdTd.className = 'tabular-nums';
    createdTd.textContent = formatDateTime(meeting.created_at);

    const actionsTd = document.createElement('td');
    actionsTd.className = 'num';
    actionsTd.style.whiteSpace = 'nowrap';

    if (meeting.status !== 'ended') {
      const joinLink = document.createElement('a');
      joinLink.href = `/meeting/${encodeURIComponent(meeting.meeting_code)}/`;
      joinLink.className = 'btn btn-primary btn-sm';
      joinLink.textContent = 'Enter';
      actionsTd.appendChild(joinLink);

      const wbLink = document.createElement('a');
      wbLink.href = `/meeting/${encodeURIComponent(meeting.meeting_code)}/?view=whiteboard`;
      wbLink.className = 'btn btn-secondary btn-sm';
      wbLink.style.marginLeft = '6px';
      wbLink.textContent = 'Whiteboard';
      actionsTd.appendChild(wbLink);
    }

    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'btn btn-ghost btn-sm';
    copyBtn.style.marginLeft = '6px';
    copyBtn.textContent = 'Copy Link';
    copyBtn.addEventListener('click', async () => {
      const inviteUrl = `${window.location.origin}/meeting/${meeting.meeting_code}/`;
      await copyToClipboard(inviteUrl);
      showToast('Invitation link copied.', 'success');
    });
    actionsTd.appendChild(copyBtn);

    const recordsBtn = document.createElement('button');
    recordsBtn.type = 'button';
    recordsBtn.className = 'btn btn-secondary btn-sm';
    recordsBtn.style.marginLeft = '6px';
    recordsBtn.textContent = 'Records';
    recordsBtn.addEventListener('click', () => {
      recordsModal?.open(meeting, 'attendance');
    });
    actionsTd.appendChild(recordsBtn);

    if (meeting.is_host && meeting.status !== 'ended') {
      const endBtn = document.createElement('button');
      endBtn.type = 'button';
      endBtn.className = 'btn btn-danger btn-sm';
      endBtn.style.marginLeft = '6px';
      endBtn.textContent = 'End';
      endBtn.addEventListener('click', async () => {
        endBtn.disabled = true;
        try {
          await apiRequest(`/meetings/${encodeURIComponent(meeting.meeting_code)}/end/`, { method: 'POST' });
          showToast('Meeting ended.', 'success');
          await loadDashboard();
        } catch (err) {
          showToast(err.message, 'error');
          endBtn.disabled = false;
        }
      });
      actionsTd.appendChild(endBtn);
    }

    tr.append(titleTd, codeTd, statusTd, hostTd, countTd, createdTd, actionsTd);
    tbody.appendChild(tr);
  });

  container.innerHTML = '';
  container.appendChild(table);
}

function setMeetingFilter(filter) {
  activeFilter = filter;
  document.querySelectorAll('#meetings-history [data-meeting-filter]').forEach((button) => {
    button.classList.toggle('is-active', button.getAttribute('data-meeting-filter') === filter);
  });
  renderMeetingsTable();
}

function renderRecentFiles(files) {
  const container = document.getElementById('recent-files-list');
  if (!container) return;

  if (!files || !files.length) {
    container.innerHTML = '<p class="field-hint">No shared files across your meetings yet.</p>';
    return;
  }

  container.innerHTML = '';
  files.slice(0, 5).forEach((file) => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--border-hairline);';

    const info = document.createElement('div');
    info.style.minWidth = '0';
    const name = document.createElement('div');
    name.style.cssText = 'font-weight:600;font-size:0.875rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    name.textContent = file.original_name;

    const meta = document.createElement('div');
    meta.className = 'meta-line';
    meta.innerHTML = `<span class="mono">${file.meeting_code}</span><span class="meta-sep">·</span><span class="tabular-nums">${formatBytes(file.file_size)}</span>`;
    info.append(name, meta);

    const dlBtn = document.createElement('button');
    dlBtn.type = 'button';
    dlBtn.className = 'btn btn-secondary btn-sm';
    dlBtn.textContent = 'Download';
    dlBtn.addEventListener('click', () => downloadAuthorizedFile(file.id, file.original_name));

    row.append(info, dlBtn);
    container.appendChild(row);
  });
}

async function downloadAuthorizedFile(fileId, filename) {
  try {
    const token = getAccessToken();
    const res = await fetch(apiUrl(`/api/v1/files/${fileId}/download/`), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error('Unable to download file.');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    downloadBlobUrl(url, filename);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function loadDashboard() {
  if (dashboardLoadPromise) return dashboardLoadPromise;

  dashboardLoadPromise = (async () => {
    try {
      const data = await apiRequest('/meetings/dashboard/');
      document.getElementById('stat-total-meetings').textContent = String(data.stats.total_meetings ?? 0);
      document.getElementById('stat-active-meetings').textContent = String(data.stats.active_meetings ?? 0);
      document.getElementById('stat-hosted-meetings').textContent = String(data.stats.hosted_meetings ?? 0);
      document.getElementById('stat-shared-files').textContent = String(data.stats.shared_files ?? 0);

      allMeetings = data.meetings || [];
      renderMeetingsTable();
      renderRecentFiles(data.recent_files || []);
    } catch (err) {
      if (err.status === 401) {
        clearAuthStorage();
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.replace(`/login/?next=${next}`);
        return;
      }

      const meetingContainer = document.getElementById('meetings-table-container');
      const filesContainer = document.getElementById('recent-files-list');
      if (meetingContainer) meetingContainer.textContent = 'Could not load meeting history.';
      if (filesContainer) filesContainer.textContent = 'Could not load shared files.';
      showToast(err.message || 'Failed to load dashboard data.', 'error');
    }
  })();

  try {
    await dashboardLoadPromise;
  } finally {
    dashboardLoadPromise = null;
  }
}

function initCreateModal() {
  const modal = document.getElementById('create-meeting-modal');
  const openBtn = document.getElementById('open-create-modal-btn');
  const closeBtn = document.getElementById('close-create-modal-btn');
  const cancelBtn = document.getElementById('cancel-create-modal-btn');
  const form = document.getElementById('create-meeting-form');
  const titleInput = document.getElementById('meeting-title-input');

  const closeModal = () => modal.classList.add('hidden');
  const openModal = () => {
    modal.classList.remove('hidden');
    titleInput.focus();
  };

  openBtn?.addEventListener('click', openModal);
  closeBtn?.addEventListener('click', closeModal);
  cancelBtn?.addEventListener('click', closeModal);
  modal?.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    setFieldError('meeting-title-input', 'err-meeting-title', '');
    setFieldError('meeting-admission-deadline-input', 'err-meeting-deadline', '');
    const title = titleInput.value.trim();
    const scheduledVal = document.getElementById('meeting-scheduled-input').value;
    const deadlineVal = document.getElementById('meeting-admission-deadline-input')?.value;

    if (title.length < 2) {
      setFieldError('meeting-title-input', 'err-meeting-title', 'Enter a meeting title (at least 2 characters).');
      return;
    }

    if (scheduledVal && deadlineVal && new Date(deadlineVal) <= new Date(scheduledVal)) {
      setFieldError(
        'meeting-admission-deadline-input',
        'err-meeting-deadline',
        'Admission deadline must be after the scheduled start time.',
      );
      return;
    }

    const submitBtn = document.getElementById('create-meeting-submit-btn');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Creating...';

    try {
      const payload = { title };
      if (scheduledVal) {
        payload.scheduled_at = new Date(scheduledVal).toISOString();
      }
      if (deadlineVal) {
        payload.admission_deadline = new Date(deadlineVal).toISOString();
      }
      const meeting = await apiRequest('/meetings/', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      closeModal();
      showToast('Meeting created.', 'success');
      if (meeting.status === 'active') {
        window.location.href = `/meeting/${encodeURIComponent(meeting.meeting_code)}/`;
      } else {
        await loadDashboard();
      }
    } catch (err) {
      setFieldError('meeting-title-input', 'err-meeting-title', err.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Create & Enter Room';
    }
  });
}

async function initDashboard() {
  const user = await requireAuth();
  if (!user) return;

  const welcome = document.getElementById('dashboard-welcome');
  if (welcome) {
    const displayName = user.full_name?.trim() || user.username || 'there';
    welcome.textContent = `Welcome back, ${displayName}`;
  }

  document.querySelectorAll('#meetings-history [data-meeting-filter]').forEach((btn) => {
    btn.addEventListener('click', () => {
      setMeetingFilter(btn.getAttribute('data-meeting-filter'));
    });
  });

  document.querySelectorAll('[data-dashboard-filter]').forEach((card) => {
    card.addEventListener('click', () => {
      setMeetingFilter(card.getAttribute('data-dashboard-filter'));
    });
  });

  const joinForm = document.getElementById('dashboard-join-form');
  const codeInput = document.getElementById('dashboard-meeting-code');
  joinForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = extractMeetingCode(codeInput.value);
    if (!code) {
      showToast('Enter a valid meeting ID.', 'warning');
      return;
    }
    window.location.href = `/meeting/${encodeURIComponent(code)}/`;
  });

  recordsModal = initMeetingRecordsModal();
  initCreateModal();
  await loadDashboard();

  window.addEventListener('focus', loadDashboard);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') loadDashboard();
  });
}

document.addEventListener('DOMContentLoaded', initDashboard);
