import { API_BASE, STORAGE_KEYS } from './config.js';

let refreshPromise = null;

export function getAccessToken() {
  return localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
}

export function getRefreshToken() {
  return localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN);
}

export function setTokens({ access, refresh }) {
  if (access) localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, access);
  if (refresh) localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, refresh);
}

export function clearAuthStorage() {
  localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN);
  localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN);
  localStorage.removeItem(STORAGE_KEYS.USER);
}

export async function refreshAccessToken() {
  const refresh = getRefreshToken();
  if (!refresh) return null;

  if (!refreshPromise) {
    refreshPromise = fetch(`${API_BASE}/auth/token/refresh/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh }),
    })
      .then(async (res) => {
        if (!res.ok) {
          clearAuthStorage();
          return null;
        }
        const data = await res.json();
        setTokens({ access: data.access, refresh: data.refresh });
        return data.access;
      })
      .catch(() => null)
      .finally(() => {
        refreshPromise = null;
      });
  }

  return refreshPromise;
}

export async function apiRequest(path, options = {}, { auth = true, retryOn401 = true } = {}) {
  const url = path.startsWith('http') || path.startsWith('/api') ? path : `${API_BASE}${path}`;
  const headers = new Headers(options.headers || {});

  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  if (auth) {
    const token = getAccessToken();
    if (token) {
      headers.set('Authorization', `Bearer ${token}`);
    }
  }

  const response = await fetch(url, {
    ...options,
    headers,
  });

  if (response.status === 401 && auth && retryOn401) {
    const newAccess = await refreshAccessToken();
    if (newAccess) {
      return apiRequest(path, options, { auth, retryOn401: false });
    }
  }

  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json')
    ? await response.json().catch(() => ({}))
    : await response.text().catch(() => '');

  if (!response.ok) {
    const message = extractErrorMessage(data, response.status);
    const error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }

  return data;
}

export function extractErrorMessage(data, status) {
  if (!data) return `Request failed (${status})`;
  if (typeof data === 'string') return data || `Request failed (${status})`;
  if (data.detail) return data.detail;
  if (data.message) return data.message;
  if (Array.isArray(data.non_field_errors) && data.non_field_errors.length) {
    return data.non_field_errors[0];
  }
  for (const [key, val] of Object.entries(data)) {
    if (Array.isArray(val) && val.length) return `${key}: ${val[0]}`;
    if (typeof val === 'string') return `${key}: ${val}`;
  }
  return `Request failed (${status})`;
}

export function uploadWithProgress(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const url = path.startsWith('/api') ? path : `${API_BASE}${path}`;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);

    const token = getAccessToken();
    if (token) {
      xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    }

    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && typeof onProgress === 'function') {
        const percent = Math.round((event.loaded / event.total) * 100);
        onProgress(percent);
      }
    });

    xhr.addEventListener('load', async () => {
      let parsed = {};
      try {
        parsed = JSON.parse(xhr.responseText || '{}');
      } catch {
        parsed = {};
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(parsed);
      } else if (xhr.status === 401) {
        const refreshed = await refreshAccessToken();
        if (refreshed) {
          uploadWithProgress(path, formData, onProgress).then(resolve).catch(reject);
        } else {
          reject(new Error('Session expired. Please sign in again.'));
        }
      } else {
        reject(new Error(extractErrorMessage(parsed, xhr.status)));
      }
    });

    xhr.addEventListener('error', () => {
      reject(new Error('Network error while uploading file.'));
    });

    xhr.send(formData);
  });
}
