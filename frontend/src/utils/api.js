import axios from 'axios';

const API_BASE = import.meta.env.VITE_API_URL || '/api';

const api = axios.create({
  baseURL: API_BASE,
  timeout: 15000,
});

let telegramUserId = null;

export function setTelegramUserId(id) {
  telegramUserId = id;
}

api.interceptors.request.use((config) => {
  if (telegramUserId) {
    config.headers['X-Telegram-User-Id'] = telegramUserId;
  }
  return config;
});

export default api;
