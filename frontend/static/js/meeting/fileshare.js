import { ALLOWED_FILE_EXTENSIONS, MAX_UPLOAD_BYTES } from '../core/config.js';
import { apiRequest, apiUrl, getAccessToken, uploadWithProgress } from '../core/api.js';
import { downloadBlobUrl, formatBytes, formatDateTime, showToast } from '../core/ui.js';

export class MeetingFileShare {
  constructor(meetingCode, socketClient) {
    this.meetingCode = meetingCode;
    this.socketClient = socketClient;
    this.files = [];
    this.canUpload = true;

    this.fileInput = document.getElementById('meeting-file-input');
    this.uploadBtn = document.getElementById('upload-file-btn');
    this.progressTrack = document.getElementById('upload-progress-track');
    this.progressFill = document.getElementById('upload-progress-fill');
    this.statusText = document.getElementById('upload-status-text');
    this.listEl = document.getElementById('shared-files-list');

    this.uploadBtn?.addEventListener('click', () => this.handleUpload());
  }

  setAllowStudentUpload(allowed, isTeacher = false) {
    this.canUpload = Boolean(isTeacher || allowed);
    if (this.fileInput) this.fileInput.disabled = !this.canUpload;
    if (this.uploadBtn) this.uploadBtn.disabled = !this.canUpload;
    if (this.statusText) {
      this.statusText.textContent = this.canUpload
        ? 'Allowed: PDF, images, docs, spreadsheets, archives (max 25 MB).'
        : 'File uploads are restricted to the teacher by room policy.';
    }
  }

  async loadFiles() {
    try {
      const data = await apiRequest(`/meetings/${encodeURIComponent(this.meetingCode)}/files/`);
      this.files = Array.isArray(data) ? data : (data.results || []);
      this.render();
    } catch (err) {
      showToast(err.message || 'Could not load shared files.', 'error');
    }
  }

  addRemoteFile(fileObj) {
    if (!fileObj) return;
    if (this.files.some((f) => f.id === fileObj.id)) return;
    this.files.unshift(fileObj);
    this.render();
    showToast(`New file shared: ${fileObj.original_name}`, 'info');
  }

  validateFile(file) {
    if (!file) return 'Choose a file to upload.';
    if (file.size > MAX_UPLOAD_BYTES) {
      return `File exceeds ${formatBytes(MAX_UPLOAD_BYTES)} limit.`;
    }
    const idx = file.name.lastIndexOf('.');
    const ext = idx >= 0 ? file.name.slice(idx).toLowerCase() : '';
    if (!ALLOWED_FILE_EXTENSIONS.includes(ext)) {
      return `File extension "${ext || 'unknown'}" is not permitted.`;
    }
    return null;
  }

  async handleUpload() {
    if (!this.canUpload) {
      showToast('File uploads are restricted to the teacher by room policy.', 'warning');
      return;
    }
    const file = this.fileInput?.files?.[0];
    const validationErr = this.validateFile(file);
    if (validationErr) {
      showToast(validationErr, 'warning');
      return;
    }

    const formData = new FormData();
    formData.append('file', file);

    this.uploadBtn.disabled = true;
    this.progressTrack?.classList.remove('hidden');
    this.progressFill.style.width = '0%';
    this.statusText.textContent = `Uploading ${file.name}...`;

    try {
      const uploaded = await uploadWithProgress(
        `/meetings/${encodeURIComponent(this.meetingCode)}/files/`,
        formData,
        (pct) => {
          this.progressFill.style.width = `${pct}%`;
          this.statusText.textContent = `Uploading ${file.name} (${pct}%)`;
        }
      );

      this.files.unshift(uploaded);
      this.render();
      this.fileInput.value = '';
      this.statusText.textContent = `Uploaded ${uploaded.original_name}.`;
      this.socketClient.emit('file_shared', { file: uploaded });
      showToast('File uploaded and shared with meeting participants.', 'success');
    } catch (err) {
      this.statusText.textContent = err.message;
      showToast(err.message, 'error');
    } finally {
      this.uploadBtn.disabled = false;
      setTimeout(() => {
        this.progressTrack?.classList.add('hidden');
        this.progressFill.style.width = '0%';
      }, 1000);
    }
  }

  async downloadFile(fileObj) {
    try {
      const res = await fetch(apiUrl(`/api/v1/files/${fileObj.id}/download/`), {
        headers: { Authorization: `Bearer ${getAccessToken()}` },
      });
      if (!res.ok) {
        throw new Error('Authorized download failed.');
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      downloadBlobUrl(url, fileObj.original_name);
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  render() {
    if (!this.listEl) return;
    if (!this.files.length) {
      this.listEl.innerHTML = '<p class="meeting-meta">No files shared in this meeting yet.</p>';
      return;
    }

    this.listEl.innerHTML = '';
    this.files.forEach((f) => {
      const item = document.createElement('div');
      item.className = 'file-item';

      const top = document.createElement('div');
      top.className = 'file-item-top';
      const name = document.createElement('div');
      name.className = 'file-name';
      name.textContent = f.original_name;
      const dlBtn = document.createElement('button');
      dlBtn.type = 'button';
      dlBtn.className = 'btn btn-secondary btn-sm';
      dlBtn.textContent = 'Download';
      dlBtn.addEventListener('click', () => this.downloadFile(f));
      top.append(name, dlBtn);

      const meta = document.createElement('div');
      meta.className = 'file-meta tabular-nums';
      meta.textContent = `${formatBytes(f.file_size)} · ${f.uploader_username || 'Participant'} · ${formatDateTime(f.uploaded_at)}`;

      item.append(top, meta);
      this.listEl.appendChild(item);
    });
  }
}
