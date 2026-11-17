export const API_BASE = '/api/v1';

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
