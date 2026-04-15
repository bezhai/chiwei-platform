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

// prom-client types (optional peer dependency)
interface PromCounter {
    inc(labels: Record<string, string>): void;
}
interface PromHistogram {
    observe(labels: Record<string, string>, value: number): void;
}
interface PromRegistry {
    registerMetric(metric: any): void;
}

// 模块级 metrics 实例（懒初始化，所有 LaneRouter 实例共享）
let outboundRequestsTotal: PromCounter | null = null;
let outboundRequestDuration: PromHistogram | null = null;
let metricsInitialized = false;

function initMetrics(registry: PromRegistry): void {
    if (metricsInitialized) return;
    try {
        // 动态 require prom-client（optional peer dep）
        const prom = require('prom-client');
        outboundRequestsTotal = new prom.Counter({
            name: 'http_outbound_requests_total',
            help: 'Total outbound HTTP requests via LaneRouter',
            labelNames: ['target_service', 'method', 'status'],
            registers: [registry],
        });
        outboundRequestDuration = new prom.Histogram({
            name: 'http_outbound_request_duration_seconds',
            help: 'Outbound HTTP request duration in seconds',
            labelNames: ['target_service', 'method'],
            registers: [registry],
        });
        metricsInitialized = true;
    } catch {
        // prom-client not available, metrics disabled
    }
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

    constructor(registryUrl: string, pollInterval = 30_000, promRegistry?: PromRegistry) {
        this.registryUrl = registryUrl.replace(/\/+$/, '');
        this.pollInterval = pollInterval;

        if (promRegistry) {
            initMetrics(promRegistry);
        }

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
     *
     * sidecar 模式下不再拼接泳道后缀，始终返回 http://service:port/path。
     * 泳道路由由 sidecar 根据 x-ctx-lane header 透明处理。
     */
    resolveUrl(service: string, path = ''): string {
        const info = this.services[service];
        const port = info?.port ?? 0;

        if (port && port !== 80) {
            return `http://${service}:${port}${path}`;
        }
        return `http://${service}${path}`;
    }

    /**
     * 返回 http://host:port（不含 path）
     */
    baseUrl(service: string): string {
        return this.resolveUrl(service, '');
    }

    /**
     * 获取当前 context 需要注入的 headers
     */
    private getContextHeaders(): Record<string, string> {
        const headers: Record<string, string> = {};
        const lane = context.get<string>('lane');
        if (lane) headers['x-ctx-lane'] = lane;
        const traceId = context.getTraceId();
        if (traceId) headers['X-Trace-Id'] = traceId;
        const appName = context.get<string>('botName');
        if (appName) headers['X-App-Name'] = appName;
        return headers;
    }

    /**
     * fetch 封装 — 自动解析 URL + 注入 x-ctx-lane/trace headers
     */
    async fetch(
        service: string,
        path: string,
        init?: RequestInit,
    ): Promise<Response> {
        const url = this.resolveUrl(service, path);
        const contextHeaders = this.getContextHeaders();

        const mergedHeaders = {
            ...contextHeaders,
            ...(init?.headers as Record<string, string>),
        };

        const method = init?.method?.toUpperCase() || 'GET';
        const start = performance.now();
        let status = 'network_error';

        try {
            const resp = await fetch(url, {
                ...init,
                headers: mergedHeaders,
            });
            status = String(resp.status);
            return resp;
        } finally {
            const duration = (performance.now() - start) / 1000;
            outboundRequestsTotal?.inc({ target_service: service, method, status });
            outboundRequestDuration?.observe({ target_service: service, method }, duration);
        }
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
            // Attach start time for duration tracking
            (config as any).__startTime = performance.now();
            return config;
        });

        // Response interceptor: record success metrics
        client.interceptors.response.use(
            (response) => {
                const start = (response.config as any).__startTime;
                if (start) {
                    const duration = (performance.now() - start) / 1000;
                    const method = (response.config.method || 'get').toUpperCase();
                    outboundRequestsTotal?.inc({ target_service: service, method, status: String(response.status) });
                    outboundRequestDuration?.observe({ target_service: service, method }, duration);
                }
                return response;
            },
            (error) => {
                const config = error.config;
                const start = config?.__startTime;
                if (start) {
                    const duration = (performance.now() - start) / 1000;
                    const method = (config.method || 'get').toUpperCase();
                    const status = error.response ? String(error.response.status) : 'network_error';
                    outboundRequestsTotal?.inc({ target_service: service, method, status });
                    outboundRequestDuration?.observe({ target_service: service, method }, duration);
                }
                return Promise.reject(error);
            },
        );

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
