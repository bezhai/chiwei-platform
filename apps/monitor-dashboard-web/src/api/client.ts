import axios from 'axios';

const TOKEN_KEY = 'monitor_dashboard_token';

export const getToken = () => localStorage.getItem(TOKEN_KEY) || '';

export const setToken = (token: string) => {
  localStorage.setItem(TOKEN_KEY, token);
};

export const clearToken = () => {
  localStorage.removeItem(TOKEN_KEY);
};

export const api = axios.create({
  baseURL: '/dashboard/api',
});

api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      clearToken();
      if (window.location.pathname !== '/dashboard/login') {
        window.location.href = '/dashboard/login';
      }
    }
    return Promise.reject(error);
  }
);
