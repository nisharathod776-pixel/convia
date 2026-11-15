import { STORAGE_KEYS } from './config.js';
import { apiRequest, clearAuthStorage, getAccessToken, getRefreshToken, refreshAccessToken } from './api.js';

export function getStoredUser() {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.USER);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function setStoredUser(user) {
  if (user) {
    localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(user));
  }
}

export async function fetchCurrentUser() {
  let token = getAccessToken();
  if (!token) {
    token = await refreshAccessToken();
  }
  if (!token) return null;

  try {
    const user = await apiRequest('/auth/me/');
    setStoredUser(user);
    return user;
  } catch {
    clearAuthStorage();
    return null;
  }
}

export async function requireAuth() {
  const user = await fetchCurrentUser();
  if (!user) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.replace(`/login/?next=${next}`);
    return null;
  }
  hydrateSidebar(user);
  return user;
}

export function hydrateSidebar(user) {
  const nameEl = document.getElementById('sidebar-user-name');
  const emailEl = document.getElementById('sidebar-user-email');
  const adminLink = document.getElementById('sidebar-admin-link');

  if (nameEl) nameEl.textContent = user.full_name || user.username;
  if (emailEl) emailEl.textContent = user.email;
  if (adminLink && (user.is_staff || user.role === 'admin')) {
    adminLink.classList.remove('hidden');
  }

  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn && !logoutBtn.dataset.bound) {
    logoutBtn.dataset.bound = 'true';
    logoutBtn.addEventListener('click', logoutUser);
  }
}

export async function logoutUser() {
  const refresh = getRefreshToken();
  try {
    if (refresh) {
      await apiRequest('/auth/logout/', {
        method: 'POST',
        body: JSON.stringify({ refresh }),
      });
    }
  } catch {
    // Ignore errors during logout cleanup
  } finally {
    clearAuthStorage();
    window.location.replace('/login/');
  }
}

export function extractMeetingCode(rawInput) {
  const trimmed = String(rawInput || '').trim();
  if (!trimmed) return '';
  try {
    if (trimmed.includes('://') || trimmed.includes('/meeting/')) {
      const url = trimmed.startsWith('http') ? new URL(trimmed) : new URL(trimmed, window.location.origin);
      const match = url.pathname.match(/\/meeting\/([a-zA-Z0-9_-]+)/);
      if (match) return match[1].toLowerCase();
    }
  } catch {
    // Fallback to raw code
  }
  return trimmed.toLowerCase();
}
