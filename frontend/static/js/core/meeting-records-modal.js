import { apiRequest, apiUrl, getAccessToken } from './api.js';
import { downloadBlobUrl, formatBytes, formatDateTime, showToast } from './ui.js';
import {
  createRecordingPlayer,
  releaseRecordingPreview,
} from '../meeting/recorder.js';

function formatElapsedSeconds(totalSeconds) {
  const secs = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  if (mins >= 60) {
    const hrs = Math.floor(mins / 60);
    const m = mins % 60;
    return `${hrs}h ${String(m).padStart(2, '0')}m`;
  }
  return `${mins}m ${String(rem).padStart(2, '0')}s`;
}

export function initMeetingRecordsModal() {
  const modal = document.getElementById('meeting-records-modal');
  if (!modal) {
    return { open: () => {} };
  }

  const titleEl = document.getElementById('records-modal-title');
  const subtitleEl = document.getElementById('records-modal-subtitle');
  const closeBtn = document.getElementById('close-records-modal-btn');
  const exportBtn = document.getElementById('records-export-csv-btn');
  const attPanel = document.getElementById('records-attendance-panel');
  const recPanel = document.getElementById('records-recordings-panel');
  const summaryEl = document.getElementById('records-attendance-summary');
  const attBodyEl = document.getElementById('records-attendance-body');
  const recBodyEl = document.getElementById('records-recordings-body');
  const tabBtns = Array.from(modal.querySelectorAll('[data-records-tab]'));

  let activeMeetingCode = null;
  let activeCanEdit = false;
  let activeBlobUrls = [];
  const activePreviewUrls = new Map();
  const activePreviewPlayers = new Map();
  let modalGeneration = 0;
  let playbackGeneration = 0;

  function revokeActiveUrls() {
    playbackGeneration += 1;
    const previewUrls = new Set(activePreviewUrls.values());
    activePreviewPlayers.forEach((player, recordingId) => {
      releaseRecordingPreview(player, activePreviewUrls.get(recordingId));
    });
    activePreviewPlayers.clear();
    activeBlobUrls.forEach((url) => {
      if (!previewUrls.has(url)) URL.revokeObjectURL(url);
    });
    activeBlobUrls = [];
    activePreviewUrls.clear();
  }

  function revokePreviewUrl(recordingId) {
    const url = activePreviewUrls.get(recordingId);
    if (!url) return;
    const player = activePreviewPlayers.get(recordingId);
    releaseRecordingPreview(player, url);
    activePreviewPlayers.delete(recordingId);
    activeBlobUrls = activeBlobUrls.filter((activeUrl) => activeUrl !== url);
    activePreviewUrls.delete(recordingId);
  }

  function closeModal() {
    modalGeneration += 1;
    revokeActiveUrls();
    if (recBodyEl) recBodyEl.innerHTML = '';
    if (attBodyEl) attBodyEl.innerHTML = '';
    modal.classList.add('hidden');
    activeMeetingCode = null;
  }

  function setTab(tab) {
    if (tab !== 'recordings') revokeActiveUrls();
    tabBtns.forEach((btn) => {
      btn.classList.toggle('is-active', btn.getAttribute('data-records-tab') === tab);
    });
    attPanel?.classList.toggle('hidden', tab !== 'attendance');
    recPanel?.classList.toggle('hidden', tab !== 'recordings');
  }

  tabBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      setTab(btn.getAttribute('data-records-tab') || 'attendance');
    });
  });

  closeBtn?.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  async function fetchBlobUrl(recordingId, inline = true) {
    const token = getAccessToken();
    const q = inline ? '?inline=1' : '';
    const res = await fetch(apiUrl(`/api/v1/meetings/recordings/${encodeURIComponent(recordingId)}/download/${q}`), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error('Unable to load recording.');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    if (inline) activeBlobUrls.push(url);
    return url;
  }

  exportBtn?.addEventListener('click', async () => {
    if (!activeMeetingCode) return;
    try {
      const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(activeMeetingCode)}/attendance/export/`), {
        headers: { Authorization: `Bearer ${getAccessToken()}` },
      });
      if (!res.ok) throw new Error('Attendance CSV export failed.');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      downloadBlobUrl(url, `attendance-${activeMeetingCode}.csv`);
    } catch (err) {
      showToast(err.message || 'Export failed.', 'error');
    }
  });

  async function loadAttendance(meetingCode) {
    if (!attBodyEl || !summaryEl) return;
    attBodyEl.innerHTML = '<div class="skeleton" style="height: 120px;"></div>';
    summaryEl.innerHTML = '';

    try {
      const report = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/attendance/`);
      activeCanEdit = Boolean(report.is_teacher);
      exportBtn?.classList.toggle('hidden', !report.can_export);

      const s = report.summary || {};
      const stats = [
        ['Total', s.total ?? 0],
        ['Present', s.present ?? 0],
        ['Late', s.late ?? 0],
        ['Excused', s.excused ?? 0],
        ['Absent', (s.absent ?? 0) + (s.rejected ?? 0) + (s.removed ?? 0)],
      ];
      stats.forEach(([label, val]) => {
        const card = document.createElement('div');
        card.className = 'records-summary-card';
        const lbl = document.createElement('span');
        lbl.className = 'records-summary-label';
        lbl.textContent = label;
        const num = document.createElement('span');
        num.className = 'records-summary-value tabular-nums';
        num.textContent = String(val);
        card.append(lbl, num);
        summaryEl.appendChild(card);
      });

      const records = report.records || [];
      if (!records.length) {
        attBodyEl.innerHTML = '<div class="empty-state"><p>No attendance entries recorded for this meeting.</p></div>';
        return;
      }

      const table = document.createElement('table');
      table.className = 'data-table';
      table.innerHTML = `
        <thead>
          <tr>
            <th>Participant</th>
            <th>Role</th>
            <th>Status</th>
            <th>First Joined</th>
            <th class="num">Joins</th>
            <th class="num">Duration</th>
            <th>${activeCanEdit ? 'Override / Note' : 'Note'}</th>
          </tr>
        </thead>
        <tbody></tbody>
      `;
      const tbody = table.querySelector('tbody');

      records.forEach((rec) => {
        const tr = document.createElement('tr');

        const nameTd = document.createElement('td');
        const strong = document.createElement('strong');
        strong.textContent = rec.full_name || rec.username;
        const sub = document.createElement('div');
        sub.className = 'field-hint';
        sub.textContent = `@${rec.username}`;
        nameTd.append(strong, sub);

        const roleTd = document.createElement('td');
        roleTd.textContent = rec.meeting_role;

        const statusTd = document.createElement('td');
        const stSpan = document.createElement('span');
        stSpan.className = 'status-text';
        stSpan.textContent = rec.status + (rec.is_connected ? ' (Live)' : '');
        statusTd.appendChild(stSpan);

        const joinedTd = document.createElement('td');
        joinedTd.className = 'tabular-nums';
        joinedTd.textContent = formatDateTime(rec.first_joined_at);

        const joinsTd = document.createElement('td');
        joinsTd.className = 'num tabular-nums';
        joinsTd.textContent = String(rec.join_count ?? 0);

        const durTd = document.createElement('td');
        durTd.className = 'num tabular-nums';
        durTd.textContent = rec.duration_formatted || '00:00:00';

        const editTd = document.createElement('td');
        if (activeCanEdit) {
          const wrap = document.createElement('div');
          wrap.className = 'records-inline-edit';
          const sel = document.createElement('select');
          sel.className = 'form-control';
          [
            ['', 'Auto'],
            ['present', 'Present'],
            ['late', 'Late'],
            ['excused', 'Excused'],
            ['absent', 'Absent'],
          ].forEach(([val, label]) => {
            const opt = document.createElement('option');
            opt.value = val;
            opt.textContent = label;
            opt.selected = (rec.status_override || '') === val;
            sel.appendChild(opt);
          });

          const noteInput = document.createElement('input');
          noteInput.type = 'text';
          noteInput.className = 'form-control';
          noteInput.placeholder = 'Note...';
          noteInput.value = rec.note || '';

          const saveBtn = document.createElement('button');
          saveBtn.type = 'button';
          saveBtn.className = 'btn btn-secondary btn-sm';
          saveBtn.textContent = 'Save';
          saveBtn.addEventListener('click', async () => {
            saveBtn.disabled = true;
            try {
              await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/attendance/`, {
                method: 'PATCH',
                body: JSON.stringify({
                  user_id: rec.user_id,
                  status_override: sel.value,
                  note: noteInput.value,
                }),
              });
              showToast('Attendance updated.', 'success');
              await loadAttendance(meetingCode);
            } catch (err) {
              showToast(err.message || 'Could not update attendance.', 'error');
              saveBtn.disabled = false;
            }
          });
          wrap.append(sel, noteInput, saveBtn);
          editTd.appendChild(wrap);
        } else {
          editTd.textContent = rec.note || '—';
        }

        tr.append(nameTd, roleTd, statusTd, joinedTd, joinsTd, durTd, editTd);
        tbody.appendChild(tr);
      });

      attBodyEl.innerHTML = '';
      attBodyEl.appendChild(table);
    } catch (err) {
      attBodyEl.innerHTML = `<div class="empty-state"><p>${err.message || 'Unable to load attendance.'}</p></div>`;
    }
  }

  async function loadRecordings(meetingCode, generation = modalGeneration) {
    if (!recBodyEl) return;
    revokeActiveUrls();
    recBodyEl.innerHTML = '<div class="skeleton" style="height: 120px;"></div>';

    try {
      const data = await apiRequest(`/meetings/${encodeURIComponent(meetingCode)}/recordings/`);
      if (generation !== modalGeneration || modal.classList.contains('hidden')) return;
      const recs = data.recordings || [];
      if (!recs.length) {
        recBodyEl.innerHTML = '<div class="empty-state"><p>No recordings saved for this meeting.</p></div>';
        return;
      }

      const list = document.createElement('div');
      list.className = 'rec-list';

      recs.forEach((rec) => {
        const card = document.createElement('div');
        card.className = 'rec-card';

        const head = document.createElement('div');
        head.className = 'rec-card-head';

        const info = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = rec.title;
        const meta = document.createElement('div');
        meta.className = 'field-hint tabular-nums';
        meta.textContent = `${formatElapsedSeconds(rec.duration_seconds)} · ${formatBytes(rec.file_size)} · ${rec.visibility === 'participants' ? 'All Participants' : 'Teacher Only'} · ${formatDateTime(rec.created_at)}`;
        info.append(title, meta);

        const actions = document.createElement('div');
        actions.className = 'rec-card-actions';

        const previewSlot = document.createElement('div');

        const playBtn = document.createElement('button');
        playBtn.type = 'button';
        playBtn.className = 'btn btn-primary btn-sm';
        playBtn.textContent = 'Play';
        playBtn.addEventListener('click', async () => {
          const previewGeneration = playbackGeneration;
          playBtn.disabled = true;
          try {
            revokePreviewUrl(rec.id);
            const url = await fetchBlobUrl(rec.id, true);
            if (previewGeneration !== playbackGeneration || modal.classList.contains('hidden')) {
              URL.revokeObjectURL(url);
              activeBlobUrls = activeBlobUrls.filter((activeUrl) => activeUrl !== url);
              playBtn.disabled = false;
              return;
            }
            activePreviewUrls.set(rec.id, url);
            const player = createRecordingPlayer(rec.mime_type, 'rec-player');
            player.addEventListener('error', () => {
              const fallback = document.createElement('p');
              fallback.className = 'field-hint';
              fallback.textContent = 'This browser cannot preview this recording format. Download it and open it in a compatible media player.';
              player.replaceWith(fallback);
            }, { once: true });
            player.src = url;
            previewSlot.innerHTML = '';
            previewSlot.appendChild(player);
            activePreviewPlayers.set(rec.id, player);
            playBtn.disabled = false;
          } catch (err) {
            if (previewGeneration === playbackGeneration) {
              showToast(err.message || 'Unable to load recording.', 'error');
            }
            playBtn.disabled = false;
          }
        });

        const dlBtn = document.createElement('button');
        dlBtn.type = 'button';
        dlBtn.className = 'btn btn-secondary btn-sm';
        dlBtn.textContent = 'Download';
        dlBtn.addEventListener('click', async () => {
          try {
            const url = await fetchBlobUrl(rec.id, false);
            downloadBlobUrl(url, rec.original_name || `${rec.title}.webm`);
          } catch (err) {
            showToast(err.message || 'Download failed.', 'error');
          }
        });

        actions.append(playBtn, dlBtn);

        if (activeCanEdit) {
          const visBtn = document.createElement('button');
          visBtn.type = 'button';
          visBtn.className = 'btn btn-ghost btn-sm';
          const nextVis = rec.visibility === 'participants' ? 'teacher_only' : 'participants';
          visBtn.textContent = nextVis === 'participants' ? 'Share with Class' : 'Make Teacher Only';
          visBtn.addEventListener('click', async () => {
            try {
              await apiRequest(`/meetings/recordings/${encodeURIComponent(rec.id)}/`, {
                method: 'PATCH',
                body: JSON.stringify({ visibility: nextVis }),
              });
              showToast('Recording visibility updated.', 'success');
              await loadRecordings(meetingCode);
            } catch (err) {
              showToast(err.message || 'Update failed.', 'error');
            }
          });

          const delBtn = document.createElement('button');
          delBtn.type = 'button';
          delBtn.className = 'btn btn-danger btn-sm';
          delBtn.textContent = 'Delete';
          delBtn.addEventListener('click', async () => {
            try {
              await apiRequest(`/meetings/recordings/${encodeURIComponent(rec.id)}/`, {
                method: 'DELETE',
              });
              showToast('Recording deleted.', 'info');
              await loadRecordings(meetingCode);
            } catch (err) {
              showToast(err.message || 'Delete failed.', 'error');
            }
          });
          actions.append(visBtn, delBtn);
        }

        head.append(info, actions);
        card.append(head, previewSlot);
        list.appendChild(card);
      });

      recBodyEl.innerHTML = '';
      recBodyEl.appendChild(list);
    } catch (err) {
      recBodyEl.innerHTML = `<div class="empty-state"><p>${err.message || 'Unable to load recordings.'}</p></div>`;
    }
  }

  return {
    async open(meeting, initialTab = 'attendance') {
      modalGeneration += 1;
      const generation = modalGeneration;
      revokeActiveUrls();
      activeMeetingCode = meeting.meeting_code;
      activeCanEdit = Boolean(meeting.is_host);
      if (titleEl) titleEl.textContent = meeting.title || meeting.meeting_code;
      if (subtitleEl) subtitleEl.textContent = `Meeting ID: ${meeting.meeting_code} · Status: ${meeting.status}`;
      setTab(initialTab);
      modal.classList.remove('hidden');

      await loadAttendance(meeting.meeting_code);
      await loadRecordings(meeting.meeting_code, generation);
    },
  };
}
