import axios, { AxiosInstance } from 'axios';
import { context } from '../middleware/context';

/**
 * 服务路由信息（来自 lite-registry）
 */
export interface ServiceInfo {
    lanes: string[];
    port: number;
}

/**
 * LaneRouter 配置选项
 */
export interface LaneRouterOptions {
    /** lite-registry 地址 */
    registryUrl: string;
    /** 轮询间隔（毫秒），默认 30s */
    pollInterval?: number;
}

/**
 * LaneRouter - 泳道感知的服务路由器
 *
 * 1. 后台轮询 Registry 获取服务路由表
 * 2. 根据 context 中的 lane 自动拼接 URL
 * 3. 提供 fetch() / createClient() 自动注入 URL + headers
 */
export class LaneRouter {
    private services: Record<string, ServiceInfo> = {};
    private timer: ReturnType<typeof setInterval> | null = null;
    private registryUrl: string;
    private pollInterval: number;

    constructor(registryUrl: string, pollInterval = 30_000) {
        this.registryUrl = registryUrl.replace(/\/+$/, '');
        this.pollInterval = pollInterval;

        // 立即拉取一次，然后启动轮询
        this.poll();
        this.timer = setInterval(() => this.poll(), this.pollInterval);
    }

    private async poll(): Promise<void> {
        try {
            const resp = await fetch(`${this.registryUrl}/v1/routes`);
            if (resp.ok) {
                const data = await resp.json();
                this.services = data.services ?? data;
            } else {
                console.warn(`[LaneRouter] registry responded ${resp.status}`);
            }
        } catch (err) {
            console.warn('[LaneRouter] failed to poll registry:', err);
        }
    }

    /**
     * 解析服务的完整 URL
     * @param service 服务名（如 'agent-service'）
     * @param path 请求路径（如 '/chat/sse'）
     * @param lane 可选泳道覆盖，不传则从 AsyncLocalStorage context 自动读取
     */
    resolveUrl(service: string, path = '', lane?: string): string {
        const effectiveLane = lane ?? context.get<string>('lane');
        const info = this.services[service];
        const port = info?.port ?? 0;

        let host: string;
        if (effectiveLane && effectiveLane !== 'prod' && info?.lanes?.includes(effectiveLane)) {
            host = `${service}-${effectiveLane}`;
        } else {
            host = service;
        }

        if (port && port !== 80) {
            return `http://${host}:${port}${path}`;
        }
        return `http://${host}${path}`;
    }

    /**
     * 返回 http://host:port（不含 path）
     */
    baseUrl(service: string, lane?: string): string {
        return this.resolveUrl(service, '', lane);
    }

    /**
     * 获取当前 context 需要注入的 headers
     */
    private getContextHeaders(): Record<string, string> {
        const headers: Record<string, string> = {};
        const lane = context.get<string>('lane');
        if (lane) headers['x-lane'] = lane;
        const traceId = context.getTraceId();
        if (traceId) headers['X-Trace-Id'] = traceId;
        const appName = context.get<string>('botName');
        if (appName) headers['X-App-Name'] = appName;
        return headers;
    }

    /**
     * lane-aware fetch 封装
     * 自动解析 URL + 注入 x-lane/trace headers
     */
    async fetch(
        service: string,
        path: string,
        init?: RequestInit,
        lane?: string,
    ): Promise<Response> {
        const url = this.resolveUrl(service, path, lane);
        const contextHeaders = lane
            ? { 'x-lane': lane } // 手动传 lane 时只注入 x-lane
            : this.getContextHeaders();

        const mergedHeaders = {
            ...contextHeaders,
            ...(init?.headers as Record<string, string>),
        };

        return fetch(url, {
            ...init,
            headers: mergedHeaders,
        });
    }

    /**
     * 创建绑定到某服务的 axios 客户端
     * 每次请求动态解析 baseURL + 注入 headers
     */
    createClient(service: string, options: { timeout?: number } = {}): AxiosInstance {
        const client = axios.create({
            timeout: options.timeout ?? 30_000,
        });

        client.interceptors.request.use((config) => {
            config.baseURL = this.baseUrl(service);
            const headers = this.getContextHeaders();
            for (const [key, value] of Object.entries(headers)) {
                if (value) {
                    config.headers[key] = value;
                }
            }
            return config;
        });

        return client;
    }

    /**
     * 停止轮询
     */
    stop(): void {
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = null;
        }
    }
}
