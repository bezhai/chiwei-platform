import axios, { type AxiosRequestConfig } from 'axios';

function getConfig(): { baseURL: string; headers: Record<string, string> } {
  const paasApi = process.env.DASHBOARD_PAAS_API;
  const paasToken = process.env.DASHBOARD_PAAS_TOKEN;
  if (!paasApi || !paasToken) {
    throw new Error('DASHBOARD_PAAS_API or DASHBOARD_PAAS_TOKEN not configured');
  }
  return {
    baseURL: paasApi,
    headers: { 'X-API-Key': paasToken },
  };
}

function getLarkConfig(): { baseURL: string; headers: Record<string, string> } {
  const larkApi = process.env.DASHBOARD_LARK_API || 'http://lark-proxy:3003';
  const paasToken = process.env.DASHBOARD_PAAS_TOKEN;
  if (!paasToken) {
    throw new Error('DASHBOARD_PAAS_TOKEN not configured');
  }
  return {
    baseURL: larkApi,
    headers: { 'X-API-Key': paasToken },
  };
}

const TIMEOUT = 15000;

function unwrap(data: unknown): unknown {
  if (data && typeof data === 'object' && 'data' in data) {
    return (data as Record<string, unknown>).data;
  }
  return data;
}

function createClient(configFn: () => { baseURL: string; headers: Record<string, string> }) {
  return {
    async get(path: string, params?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const config: AxiosRequestConfig = { headers, timeout: TIMEOUT, params };
      const res = await axios.get(`${baseURL}${path}`, config);
      return unwrap(res.data);
    },

    async post(path: string, body?: unknown) {
      const { baseURL, headers } = configFn();
      const config: AxiosRequestConfig = {
        headers: { ...headers, 'Content-Type': 'application/json' },
        timeout: TIMEOUT,
      };
      const res = await axios.post(`${baseURL}${path}`, body, config);
      return unwrap(res.data);
    },

    async del(path: string, params?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const config: AxiosRequestConfig = { headers, timeout: TIMEOUT, params };
      const res = await axios.delete(`${baseURL}${path}`, config);
      return unwrap(res.data);
    },
  };
}

export const paasClient = createClient(getConfig);
export const larkClient = createClient(getLarkConfig);
