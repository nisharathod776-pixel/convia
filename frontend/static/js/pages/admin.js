import { apiRequest, apiUrl, getAccessToken } from '../core/api.js';
import { requireAuth } from '../core/auth.js';
import { initMeetingRecordsModal } from '../core/meeting-records-modal.js';
import { downloadBlobUrl, formatBytes, formatDateTime, setAlert, showToast } from '../core/ui.js';

let adminData = null;
let currentAdminId = null;
let recordsModal = null;

function renderUsersTable(query = '') {
  const container = document.getElementById('admin-users-table');
  if (!container || !adminData) return;

  const q = query.trim().toLowerCase();
  const statusFilter = document.getElementById('admin-user-status-filter')?.value || 'all';
  const roleFilter = document.getElementById('admin-user-role-filter')?.value || 'all';
  const users = (adminData.users || []).filter((u) =>
    (!q || u.username.toLowerCase().includes(q) || u.email.toLowerCase().includes(q) || (u.full_name || '').toLowerCase().includes(q))
    && (statusFilter === 'all' || (statusFilter === 'active' ? u.is_active : !u.is_active))
    && (roleFilter === 'all' || (roleFilter === 'admin' ? (u.is_staff || u.role === 'admin') : !(u.is_staff || u.role === 'admin')))
  );

  if (!users.length) {
    container.innerHTML = '<div class="empty-state"><p>No matching user accounts.</p></div>';
    return;
  }

  const table = document.createElement('table');
  table.className = 'data-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>User</th>
        <th>Email</th>
        <th>Role</th>
        <th>Status</th>
        <th>Joined</th>
        <th class="num">Action</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector('tbody');

  users.forEach((u) => {
    const tr = document.createElement('tr');
    const nameTd = document.createElement('td');
    nameTd.innerHTML = `<strong></strong><div class="field-hint"></div>`;
    nameTd.querySelector('strong').textContent = u.full_name || u.username;
    nameTd.querySelector('.field-hint').textContent = `@${u.username}`;

    const emailTd = document.createElement('td');
    emailTd.textContent = u.email;

    const roleTd = document.createElement('td');
    const isAdmin = u.is_staff || u.role === 'admin';
    roleTd.textContent = isAdmin ? 'Admin' : 'Member';

    const statusTd = document.createElement('td');
    const st = document.createElement('span');
    st.className = `status-text ${u.is_active ? 'is-active' : 'is-danger'}`;
    st.textContent = u.is_active ? 'Active' : 'Deactivated';
    statusTd.appendChild(st);

    const joinedTd = document.createElement('td');
    joinedTd.className = 'tabular-nums';
    joinedTd.textContent = formatDateTime(u.date_joined);

    const actionTd = document.createElement('td');
    actionTd.className = 'num';
    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = `btn btn-sm ${u.is_active ? 'btn-danger' : 'btn-primary'}`;
    toggleBtn.textContent = u.is_active ? 'Deactivate' : 'Activate';
    if (u.id === currentAdminId) {
      toggleBtn.disabled = true;
      toggleBtn.title = 'You cannot change your own account status.';
    }
    toggleBtn.addEventListener('click', async () => {
      toggleBtn.disabled = true;
      try {
        await apiRequest(`/admin/users/${u.id}/status/`, {
          method: 'PATCH',
          body: JSON.stringify({ is_active: !u.is_active }),
        });
        showToast(`User ${u.username} ${u.is_active ? 'deactivated' : 'activated'}.`, 'success');
        await loadAdminOverview();
      } catch (err) {
        showToast(err.message, 'error');
        toggleBtn.disabled = false;
      }
    });
    actionTd.appendChild(toggleBtn);

    tr.append(nameTd, emailTd, roleTd, statusTd, joinedTd, actionTd);
    tbody.appendChild(tr);
  });

  container.innerHTML = '';
  container.appendChild(table);
}

function renderMeetingsTable() {
  const container = document.getElementById('admin-meetings-table');
  if (!container || !adminData) return;
  const meetings = adminData.meetings || [];
  if (!meetings.length) {
    container.innerHTML = '<div class="empty-state"><p>No meeting records.</p></div>';
    return;
  }

  const table = document.createElement('table');
  table.className = 'data-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>Title</th>
        <th>Code</th>
        <th>Host</th>
        <th>Status</th>
        <th class="num">Participants</th>
        <th>Created</th>
        <th class="num">Control</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector('tbody');

  meetings.forEach((m) => {
    const tr = document.createElement('tr');
    const tTd = document.createElement('td');
    tTd.textContent = m.title;
    const cTd = document.createElement('td');
    cTd.className = 'mono';
    cTd.textContent = m.meeting_code;
    const hTd = document.createElement('td');
    hTd.textContent = m.host_username;
    const sTd = document.createElement('td');
    sTd.innerHTML = `<span class="status-text is-${m.status}">${m.status}</span>`;
    const pTd = document.createElement('td');
    pTd.className = 'num tabular-nums';
    pTd.textContent = String(m.participant_count);
    const crTd = document.createElement('td');
    crTd.className = 'tabular-nums';
    crTd.textContent = formatDateTime(m.created_at);

    const actTd = document.createElement('td');
    actTd.className = 'num';
    actTd.style.whiteSpace = 'nowrap';

    const recBtn = document.createElement('button');
    recBtn.type = 'button';
    recBtn.className = 'btn btn-secondary btn-sm';
    recBtn.textContent = 'Records';
    recBtn.addEventListener('click', () => {
      recordsModal?.open(m, 'attendance');
    });
    actTd.appendChild(recBtn);

    if (m.status !== 'ended') {
      const endBtn = document.createElement('button');
      endBtn.type = 'button';
      endBtn.className = 'btn btn-danger btn-sm';
      endBtn.style.marginLeft = '6px';
      endBtn.textContent = 'Force End';
      endBtn.addEventListener('click', async () => {
        endBtn.disabled = true;
        try {
          await apiRequest(`/meetings/${encodeURIComponent(m.meeting_code)}/end/`, { method: 'POST' });
          showToast('Meeting ended.', 'success');
          await loadAdminOverview();
        } catch (err) {
          showToast(err.message, 'error');
          endBtn.disabled = false;
        }
      });
      actTd.appendChild(endBtn);
    }

    tr.append(tTd, cTd, hTd, sTd, pTd, crTd, actTd);
    tbody.appendChild(tr);
  });

  container.innerHTML = '';
  container.appendChild(table);
}

function renderFilesTable() {
  const container = document.getElementById('admin-files-table');
  if (!container || !adminData) return;
  const files = adminData.files || [];
  if (!files.length) {
    container.innerHTML = '<div class="empty-state"><p>No uploaded files.</p></div>';
    return;
  }

  const table = document.createElement('table');
  table.className = 'data-table';
  table.innerHTML = `<thead><tr><th>File</th><th>Meeting</th><th>Uploaded By</th><th class="num">Size</th><th class="num">Action</th></tr></thead><tbody></tbody>`;
  const tbody = table.querySelector('tbody');

  files.forEach((f) => {
    const tr = document.createElement('tr');
    const nTd = document.createElement('td');
    nTd.textContent = f.original_name;
    const mTd = document.createElement('td');
    mTd.className = 'mono';
    mTd.textContent = f.meeting_code;
    const uploaderTd = document.createElement('td');
    uploaderTd.textContent = f.uploader_username || 'Unknown';
    const sTd = document.createElement('td');
    sTd.className = 'num tabular-nums';
    sTd.textContent = formatBytes(f.file_size);

    const aTd = document.createElement('td');
    aTd.className = 'num';
    const dlBtn = document.createElement('button');
    dlBtn.type = 'button';
    dlBtn.className = 'btn btn-secondary btn-sm';
    dlBtn.textContent = 'Download';
    dlBtn.addEventListener('click', async () => {
      const res = await fetch(apiUrl(`/api/v1/files/${f.id}/download/`), {
        headers: { Authorization: `Bearer ${getAccessToken()}` },
      });
      if (!res.ok) return showToast('Download failed.', 'error');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      downloadBlobUrl(url, f.original_name);
    });
    aTd.appendChild(dlBtn);

    tr.append(nTd, mTd, uploaderTd, sTd, aTd);
    tbody.appendChild(tr);
  });

  container.innerHTML = '';
  container.appendChild(table);
}

function renderActivityTable() {
  const container = document.getElementById('admin-activity-table');
  if (!container || !adminData) return;
  const logs = adminData.activity || [];
  if (!logs.length) {
    container.innerHTML = '<div class="empty-state"><p>No activity recorded yet.</p></div>';
    return;
  }

  const table = document.createElement('table');
  table.className = 'data-table';
  table.innerHTML = `<thead><tr><th>Event</th><th>User</th><th>Time</th></tr></thead><tbody></tbody>`;
  const tbody = table.querySelector('tbody');

  logs.forEach((log) => {
    const tr = document.createElement('tr');
    const eTd = document.createElement('td');
    eTd.textContent = `${log.action}${log.detail ? ` — ${log.detail}` : ''}`;
    const uTd = document.createElement('td');
    uTd.textContent = log.username || 'system';
    const tTd = document.createElement('td');
    tTd.className = 'tabular-nums';
    tTd.textContent = formatDateTime(log.created_at);
    tr.append(eTd, uTd, tTd);
    tbody.appendChild(tr);
  });

  container.innerHTML = '';
  container.appendChild(table);
}

async function loadAdminOverview() {
  setAlert('admin-access-error', '');
  try {
    adminData = await apiRequest('/admin/overview/');
    document.getElementById('adm-total-users').textContent = String(adminData.stats.total_users ?? 0);
    document.getElementById('adm-active-users').textContent = String(adminData.stats.active_users ?? 0);
    document.getElementById('adm-deactivated-users').textContent = String(adminData.stats.deactivated_users ?? 0);
    document.getElementById('adm-total-meetings').textContent = String(adminData.stats.total_meetings ?? 0);
    document.getElementById('adm-active-meetings').textContent = String(adminData.stats.active_meetings ?? 0);
    document.getElementById('adm-completed-meetings').textContent = String(adminData.stats.completed_meetings ?? 0);
    document.getElementById('adm-total-files').textContent = String(adminData.stats.total_files ?? 0);
    document.getElementById('adm-storage-bytes').textContent = formatBytes(adminData.stats.storage_bytes ?? 0);

    const searchInput = document.getElementById('admin-user-search');
    renderUsersTable(searchInput ? searchInput.value : '');
    renderMeetingsTable();
    renderFilesTable();
    renderActivityTable();
  } catch (err) {
    setAlert('admin-access-error', err.status === 403 ? 'Administrator permissions are required to access this console.' : err.message, 'error');
  }
}

async function initAdminPage() {
  const user = await requireAuth();
  if (!user) return;
  currentAdminId = user.id;

  document.getElementById('admin-user-search')?.addEventListener('input', (e) => {
    renderUsersTable(e.target.value);
  });
  document.getElementById('admin-user-status-filter')?.addEventListener('change', () => {
    renderUsersTable(document.getElementById('admin-user-search')?.value || '');
  });
  document.getElementById('admin-user-role-filter')?.addEventListener('change', () => {
    renderUsersTable(document.getElementById('admin-user-search')?.value || '');
  });
  recordsModal = initMeetingRecordsModal();
  document.getElementById('admin-refresh-btn')?.addEventListener('click', loadAdminOverview);
  await loadAdminOverview();
}

document.addEventListener('DOMContentLoaded', initAdminPage);
