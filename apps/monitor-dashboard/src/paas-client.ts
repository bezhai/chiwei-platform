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

function getChannelConfig(): { baseURL: string; headers: Record<string, string> } {
  const channelApi = process.env.DASHBOARD_CHANNEL_API || 'http://channel-server:3000';
  const paasToken = process.env.DASHBOARD_PAAS_TOKEN;
  if (!paasToken) {
    throw new Error('DASHBOARD_PAAS_TOKEN not configured');
  }
  return {
    baseURL: channelApi,
    headers: { 'X-API-Key': paasToken },
  };
}

/**
 * agent-service：世界文档树的管理端点。
 *
 * 现有两个 client 都打不到它——paasClient 的 baseURL 是 paas-engine、channelClient 是
 * channel-server，而且两者用的都是 X-API-Key。agent-service 那几个端点的门认的是内网
 * 互信那把 Bearer（INNER_HTTP_SECRET），所以这里是第三个 client。
 *
 * 没有凭据就直接报错，不发裸请求：上游是 fail-closed 的，裸请求只会拿到 401，
 * 而这一侧在本地就能判定。
 */
export function getAgentConfig(): { baseURL: string; headers: Record<string, string> } {
  const agentApi = process.env.DASHBOARD_AGENT_API || 'http://agent-service:8000';
  const secret = process.env.INNER_HTTP_SECRET;
  if (!secret) {
    throw new Error('INNER_HTTP_SECRET not configured');
  }
  return {
    baseURL: agentApi,
    headers: { Authorization: `Bearer ${secret}` },
  };
}

const TIMEOUT = 15000;

/** paas-engine 的信封口径：{data: ...} 只取 data。 */
function unwrapEnvelope(data: unknown): unknown {
  if (data && typeof data === 'object' && 'data' in data) {
    return (data as Record<string, unknown>).data;
  }
  return data;
}

/**
 * envelope: true（默认）按 paas-engine 的信封口径拆包。
 *
 * agent-service 不是那个口径，它的响应顶层直接就是业务字段。对它拆包是危险的：
 * 上游哪天加一个 data 字段，整个响应会被替换成那个字段的值，lane 静默消失——而
 * lane 是调用方唯一能判断"这次改的是哪棵树"的东西，丢了不会有任何报错。
 */
export function createClient(
  configFn: () => { baseURL: string; headers: Record<string, string> },
  options: { envelope?: boolean } = {},
) {
  const unwrap = options.envelope === false ? (data: unknown) => data : unwrapEnvelope;
  return {
    async get(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const config: AxiosRequestConfig = { headers: { ...headers, ...extraHeaders }, timeout: TIMEOUT, params };
      const res = await axios.get(`${baseURL}${path}`, config);
      return unwrap(res.data);
    },

    async post(path: string, body?: unknown, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const config: AxiosRequestConfig = {
        headers: { ...headers, 'Content-Type': 'application/json', ...extraHeaders },
        timeout: TIMEOUT,
      };
      const res = await axios.post(`${baseURL}${path}`, body, config);
      return unwrap(res.data);
    },

    async del(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>, body?: unknown) {
      const { baseURL, headers } = configFn();
      const config: AxiosRequestConfig = {
        headers: { ...headers, 'Content-Type': 'application/json', ...extraHeaders },
        timeout: TIMEOUT,
        params,
        data: body,
      };
      const res = await axios.delete(`${baseURL}${path}`, config);
      return unwrap(res.data);
    },

    async put(path: string, body?: unknown, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const config: AxiosRequestConfig = {
        headers: { ...headers, 'Content-Type': 'application/json', ...extraHeaders },
        timeout: TIMEOUT,
      };
      const res = await axios.put(`${baseURL}${path}`, body, config);
      return unwrap(res.data);
    },
  };
}

export const paasClient = createClient(getConfig);
export const channelClient = createClient(getChannelConfig);
export const agentClient = createClient(getAgentConfig, { envelope: false });
