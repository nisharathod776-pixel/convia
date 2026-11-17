export function showToast(message, type = 'info', durationMs = 4200) {
  const region = document.getElementById('toast-region');
  if (!region) return;

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.setAttribute('role', 'status');

  const span = document.createElement('span');
  span.textContent = message;

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'toast-close';
  closeBtn.setAttribute('aria-label', 'Dismiss notification');
  closeBtn.textContent = 'Dismiss';
  closeBtn.addEventListener('click', () => toast.remove());

  toast.appendChild(span);
  toast.appendChild(closeBtn);
  region.appendChild(toast);

  if (durationMs > 0) {
    setTimeout(() => {
      toast.remove();
    }, durationMs);
  }
}

export function setAlert(elementId, message, variant = 'error') {
  const el = document.getElementById(elementId);
  if (!el) return;
  if (!message) {
    el.classList.add('hidden');
    el.textContent = '';
    return;
  }
  el.className = `alert-banner alert-${variant}`;
  el.textContent = message;
  el.classList.remove('hidden');
}

export function setFieldError(fieldId, errorId, message) {
  const input = document.getElementById(fieldId);
  const errEl = document.getElementById(errorId);
  if (input) {
    input.classList.toggle('is-invalid', Boolean(message));
  }
  if (errEl) {
    errEl.textContent = message || '';
  }
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const idx = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const val = bytes / Math.pow(1024, idx);
  return `${val.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
}

export function formatDateTime(isoString) {
  if (!isoString) return '—';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function downloadBlobUrl(url, filename) {
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  try {
    document.body.appendChild(link);
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export function getInitials(name) {
  const parts = String(name || 'User').trim().split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const ok = document.execCommand('copy');
  textarea.remove();
  return ok;
}

function initGlobalUiControls() {
  document.querySelectorAll('[data-toggle-password]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const targetId = btn.getAttribute('data-toggle-password');
      const input = document.getElementById(targetId);
      if (!input) return;
      const isPassword = input.type === 'password';
      input.type = isPassword ? 'text' : 'password';
      btn.setAttribute('aria-label', isPassword ? 'Hide password' : 'Show password');
    });
  });

  const menuBtn = document.getElementById('mobile-menu-toggle');
  const drawer = document.getElementById('mobile-drawer');
  if (menuBtn && drawer) {
    menuBtn.addEventListener('click', () => {
      const open = drawer.classList.toggle('hidden') === false;
      menuBtn.setAttribute('aria-expanded', String(open));
    });
  }
}

document.addEventListener('DOMContentLoaded', initGlobalUiControls);
