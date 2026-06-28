/**
 * QQ Bot API 客户端：access_token 刷新+缓存(singleflight) + 被动回复发文本。
 *
 * 移植自 openclaw-qqbot/src/api.ts 的 getAccessToken / sendC2CMessage / sendGroupMessage，
 * 剥掉 openclaw 的 runtime / config / upload-cache / User-Agent / markdown / 富媒体 / 流式 / 重试矩阵，
 * 只留「被动收发文本」需要的部分。msg_seq 由被动窗口管理器分配后传入，本客户端不自己生成。
 *
 * 鉴权沿用 QQ v2 文档口径：Authorization: QQBot {access_token}。
 */

export interface QQLogger {
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
}

const NOOP_LOG: QQLogger = { info: () => {}, warn: () => {}, error: () => {} };

export interface QQClientOptions {
    appId: string;
    clientSecret: string;
    /** 默认 https://api.sgroup.qq.com */
    apiBase?: string;
    /** 默认 https://bots.qq.com/app/getAppAccessToken */
    tokenUrl?: string;
    fetchImpl?: typeof fetch;
    now?: () => number;
    log?: QQLogger;
}

export interface SendOptions {
    /** 被动回复回带的原始 QQ msg_id。 */
    msgId: string;
    /** 被动窗口管理器分配的递增序号。 */
    msgSeq: number;
}

export interface SendResult {
    id?: string;
}

const DEFAULT_API_BASE = 'https://api.sgroup.qq.com';
const DEFAULT_TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken';
const API_TIMEOUT_MS = 30_000;

interface TokenCache {
    token: string;
    expiresAt: number;
}

export class QQClient {
    private readonly appId: string;
    private readonly clientSecret: string;
    private readonly apiBase: string;
    private readonly tokenUrl: string;
    private readonly fetchImpl: typeof fetch;
    private readonly now: () => number;
    private readonly log: QQLogger;

    private cache: TokenCache | null = null;
    private inFlight: Promise<string> | null = null;

    constructor(opts: QQClientOptions) {
        this.appId = String(opts.appId).trim();
        this.clientSecret = opts.clientSecret;
        this.apiBase = (opts.apiBase ?? DEFAULT_API_BASE).replace(/\/+$/, '');
        this.tokenUrl = opts.tokenUrl ?? DEFAULT_TOKEN_URL;
        this.fetchImpl = opts.fetchImpl ?? fetch;
        this.now = opts.now ?? (() => Date.now());
        this.log = opts.log ?? NOOP_LOG;
    }

    /** 取 access_token：缓存命中直接返回；过期/无缓存走 singleflight 刷新。 */
    async getAccessToken(): Promise<string> {
        const cached = this.cache;
        if (cached) {
            // 提前刷新阈值：min(5min, ttl/3)，避免短 ttl 永远判过期
            const refreshAheadMs = Math.min(5 * 60 * 1000, Math.max(0, (cached.expiresAt - this.now()) / 3));
            if (this.now() < cached.expiresAt - refreshAheadMs) {
                return cached.token;
            }
        }
        if (this.inFlight) return this.inFlight;

        this.inFlight = (async () => {
            try {
                return await this.fetchToken();
            } finally {
                this.inFlight = null;
            }
        })();
        return this.inFlight;
    }

    private async fetchToken(): Promise<string> {
        const res = await this.fetchImpl(this.tokenUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ appId: this.appId, clientSecret: this.clientSecret }),
        });
        const raw = await res.text();
        let data: { access_token?: string; expires_in?: number };
        try {
            data = JSON.parse(raw);
        } catch {
            throw new Error(`getAccessToken: failed to parse token response: ${raw.slice(0, 200)}`);
        }
        if (!res.ok || !data.access_token) {
            throw new Error(`getAccessToken failed (HTTP ${res.status}): ${raw.slice(0, 200)}`);
        }
        const expiresAt = this.now() + (data.expires_in ?? 7200) * 1000;
        this.cache = { token: data.access_token, expiresAt };
        this.log.info(`[qq-api:${this.appId}] token cached, expires in ${data.expires_in ?? 7200}s`);
        return data.access_token;
    }

    /** 强制清缓存（token 失效时调用，下次 getAccessToken 会重新拉）。 */
    clearTokenCache(): void {
        this.cache = null;
    }

    /**
     * 取 WebSocket gateway 地址：GET {apiBase}/gateway，鉴权 Authorization: QQBot {token}，
     * 返回体 { url: string }（wss 地址）。bot 用此地址主动建长连接收事件。
     */
    async getGatewayUrl(): Promise<string> {
        const token = await this.getAccessToken();
        const res = await this.fetchImpl(`${this.apiBase}/gateway`, {
            method: 'GET',
            headers: { Authorization: `QQBot ${token}` },
        });
        const raw = await res.text();
        if (!res.ok) {
            throw new Error(`getGatewayUrl failed (HTTP ${res.status}): ${raw.slice(0, 200)}`);
        }
        let data: { url?: string };
        try {
            data = JSON.parse(raw);
        } catch {
            throw new Error(`getGatewayUrl: failed to parse gateway response: ${raw.slice(0, 200)}`);
        }
        if (!data.url) {
            throw new Error(`getGatewayUrl: missing url in response: ${raw.slice(0, 200)}`);
        }
        return data.url;
    }

    async sendC2CMessage(openid: string, content: string, opts: SendOptions): Promise<SendResult> {
        return this.send(`/v2/users/${openid}/messages`, content, opts);
    }

    async sendGroupMessage(groupOpenid: string, content: string, opts: SendOptions): Promise<SendResult> {
        return this.send(`/v2/groups/${groupOpenid}/messages`, content, opts);
    }

    private async send(path: string, content: string, opts: SendOptions): Promise<SendResult> {
        const token = await this.getAccessToken();
        const body = {
            content,
            msg_type: 0,
            msg_id: opts.msgId,
            msg_seq: opts.msgSeq,
        };
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
        let res: Response;
        try {
            res = await this.fetchImpl(`${this.apiBase}${path}`, {
                method: 'POST',
                headers: {
                    Authorization: `QQBot ${token}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(body),
                signal: controller.signal,
            });
        } finally {
            clearTimeout(timer);
        }
        const raw = await res.text();
        if (!res.ok) {
            throw new Error(`QQ send failed [${path}] HTTP ${res.status}: ${raw.slice(0, 200)}`);
        }
        try {
            return JSON.parse(raw) as SendResult;
        } catch {
            return {};
        }
    }
}
