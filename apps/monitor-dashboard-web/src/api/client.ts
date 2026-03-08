import axios from 'axios';

const TOKEN_KEY = 'monitor_dashboard_token';
const LANE_KEY = 'x-lane';

// 启动时调用：检测 URL query param，存入 sessionStorage + 同步 cookie
export function initLane() {
  const params = new URLSearchParams(window.location.search);
  if (params.has(LANE_KEY)) {
    const lane = params.get(LANE_KEY)!;
    if (lane) {
      sessionStorage.setItem(LANE_KEY, lane);
      document.cookie = `${LANE_KEY}=${lane}; path=/`;
    } else {
      sessionStorage.removeItem(LANE_KEY);
      document.cookie = `${LANE_KEY}=; path=/; max-age=0`;
    }
  }
  // tab 获焦时同步 cookie（解决多 tab cookie 竞争）
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      const lane = sessionStorage.getItem(LANE_KEY);
      if (lane) document.cookie = `${LANE_KEY}=${lane}; path=/`;
      else document.cookie = `${LANE_KEY}=; path=/; max-age=0`;
    }
  });
}

export function getLane(): string | null {
  return sessionStorage.getItem(LANE_KEY);
}

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
  const lane = getLane();
  if (lane) {
    config.headers = config.headers || {};
    config.headers['x-lane'] = lane;
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
