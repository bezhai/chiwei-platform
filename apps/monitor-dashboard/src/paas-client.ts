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

/**
 * 根据 x-lane header 改写 baseURL，将 http://svc:port 变为 http://svc-{lane}:port。
 * 仅对非 prod lane 生效；无 lane 或 prod 时返回原 URL。
 */
function laneAwareUrl(baseURL: string, lane?: string): string {
  if (!lane || lane === 'prod') return baseURL;
  return baseURL.replace(
    /^(https?:\/\/)([^/:]+)(:\d+)?/,
    (_, proto, host, port) => `${proto}${host}-${lane}${port || ''}`,
  );
}

function createClient(configFn: () => { baseURL: string; headers: Record<string, string> }) {
  return {
    async get(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const url = laneAwareUrl(baseURL, extraHeaders?.['x-lane']);
      const config: AxiosRequestConfig = { headers: { ...headers, ...extraHeaders }, timeout: TIMEOUT, params };
      const res = await axios.get(`${url}${path}`, config);
      return unwrap(res.data);
    },

    async post(path: string, body?: unknown, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const url = laneAwareUrl(baseURL, extraHeaders?.['x-lane']);
      const config: AxiosRequestConfig = {
        headers: { ...headers, 'Content-Type': 'application/json', ...extraHeaders },
        timeout: TIMEOUT,
      };
      const res = await axios.post(`${url}${path}`, body, config);
      return unwrap(res.data);
    },

    async del(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const url = laneAwareUrl(baseURL, extraHeaders?.['x-lane']);
      const config: AxiosRequestConfig = { headers: { ...headers, ...extraHeaders }, timeout: TIMEOUT, params };
      const res = await axios.delete(`${url}${path}`, config);
      return unwrap(res.data);
    },

    async put(path: string, body?: unknown, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const url = laneAwareUrl(baseURL, extraHeaders?.['x-lane']);
      const config: AxiosRequestConfig = {
        headers: { ...headers, 'Content-Type': 'application/json', ...extraHeaders },
        timeout: TIMEOUT,
      };
      const res = await axios.put(`${url}${path}`, body, config);
      return unwrap(res.data);
    },
  };
}

export const paasClient = createClient(getConfig);
export const larkClient = createClient(getLarkConfig);
