const viteEnv = import.meta.env ?? {};

export const API_ORIGIN = (viteEnv.VITE_API_BASE_URL || '')
  .trim()
  .replace(/\/+$/, '');
export const API_BASE = API_ORIGIN ? `${API_ORIGIN}/api/v1` : '/api/v1';
export const SOCKET_URL = (viteEnv.VITE_SOCKET_URL || '')
  .trim()
  .replace(/\/+$/, '');
export const STORAGE_KEYS = {
  ACCESS_TOKEN: 'convia_access_token',
  REFRESH_TOKEN: 'convia_refresh_token',
  USER: 'convia_user',
};

export const ALLOWED_FILE_EXTENSIONS = [
  '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg',
  '.txt', '.md', '.csv', '.json', '.doc', '.docx', '.xls',
  '.xlsx', '.ppt', '.pptx', '.zip',
];

export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
