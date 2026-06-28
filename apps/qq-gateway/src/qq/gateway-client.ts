/**
 * QQ 官方 bot WebSocket 网关客户端：bot 主动出站长连接收事件。
 *
 * 我们是内网服务、无公网入口，腾讯 QQ 服务器 POST 不进来；QQ 官方 bot 主流走 WebSocket
 * 主动出站长连接——bot 用 access_token 主动连 QQ gateway，按 op 协议收派发事件，
 * 再经 normalize → forwardInbound 转给 channel-server。
 *
 * 协议（消息形如 {op, d, s, t}）：
 *   - op:10 Hello   → 立即发 Identify(op:2)，并按 d.heartbeat_interval 启动心跳(op:1, d=lastSeq)
 *   - op:0  Dispatch→ READY 存 session_id；RESUMED 忽略；其它 t 走 normalize → forwardInbound
 *   - op:11 Heartbeat ACK → 无需处理
 *   - op:7  Reconnect      → 关连接重连
 *   - op:9  Invalid Session→ 清 session 后重连（fresh identify）
 *   - 断线 → 退避自动重连
 *
 * 不做 resume：每次重连都从 Hello → fresh Identify 走起（spec 明确允许）。session_id 仅记录。
 *
 * 可测设计：WebSocket 构造器、定时器全部按 deps 注入，单测用 fake 驱动。
 */

import { normalizeQQEvent, type NormalizeContext } from './normalize';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import type { QQLogger } from './api';

/** WebSocket 客户端在本模块只用到的最小子集（Bun 全局 WebSocket 满足，按接口注入便于测试）。 */
export interface GatewayWebSocket {
    send(data: string): void;
    close(code?: number, reason?: string): void;
    onopen: ((ev?: unknown) => void) | null;
    onmessage: ((ev: { data: unknown }) => void | Promise<void>) | null;
    onerror: ((ev?: unknown) => void) | null;
    onclose: ((ev?: unknown) => void) | null;
}

export interface QQGatewayClientDeps {
    /** 收到事件的 bot 名，传给 normalize 让 channel-server 查 bot 配置。 */
    botName: string;
    /** 取 access_token（复用 QQClient.getAccessToken 的缓存+singleflight）。 */
    getAccessToken: () => Promise<string>;
    /** 取 wss gateway 地址（QQClient.getGatewayUrl）。 */
    getGatewayUrl: () => Promise<string>;
    /** 由 url 建 WebSocket（生产传 (url) => new WebSocket(url)，测试传 fake）。 */
    wsFactory: (url: string) => GatewayWebSocket;
    /** 把归一化后的入站消息推给 channel-server。 */
    forwardInbound: (msg: CustomInboundMessage) => Promise<void>;
    log: QQLogger;
    /** 事件订阅位，固定 1<<25 = 33554432（GROUP_AND_C2C：C2C 单聊 + 群@）。 */
    intents?: number;
    /** dispatch 归一化函数（默认 normalizeQQEvent，测试可注入）。 */
    normalize?: (eventType: string, d: unknown, ctx: NormalizeContext) => CustomInboundMessage | null;
    /** 重连退避序列（毫秒）。 */
    backoffMs?: number[];
    setIntervalImpl?: (cb: () => void, ms: number) => ReturnType<typeof setInterval>;
    clearIntervalImpl?: (handle: ReturnType<typeof setInterval>) => void;
    setTimeoutImpl?: (cb: () => void, ms: number) => ReturnType<typeof setTimeout>;
    clearTimeoutImpl?: (handle: ReturnType<typeof setTimeout>) => void;
}

const OP_DISPATCH = 0;
const OP_HEARTBEAT = 1;
const OP_IDENTIFY = 2;
const OP_RECONNECT = 7;
const OP_INVALID_SESSION = 9;
const OP_HELLO = 10;
const OP_HEARTBEAT_ACK = 11;

/** GROUP_AND_C2C：1<<25。只订这一位，覆盖 C2C 单聊 + 群@。 */
const DEFAULT_INTENTS = 33554432;
const DEFAULT_BACKOFF_MS = [1000, 2000, 5000, 10000, 30000, 60000];

interface GatewayPayload {
    op?: number;
    d?: unknown;
    s?: number;
    t?: string;
}

export class QQGatewayClient {
    private readonly botName: string;
    private readonly getAccessToken: () => Promise<string>;
    private readonly getGatewayUrl: () => Promise<string>;
    private readonly wsFactory: (url: string) => GatewayWebSocket;
    private readonly forwardInbound: (msg: CustomInboundMessage) => Promise<void>;
    private readonly log: QQLogger;
    private readonly intents: number;
    private readonly normalize: (eventType: string, d: unknown, ctx: NormalizeContext) => CustomInboundMessage | null;
    private readonly backoffMs: number[];
    private readonly setIntervalImpl: (cb: () => void, ms: number) => ReturnType<typeof setInterval>;
    private readonly clearIntervalImpl: (handle: ReturnType<typeof setInterval>) => void;
    private readonly setTimeoutImpl: (cb: () => void, ms: number) => ReturnType<typeof setTimeout>;
    private readonly clearTimeoutImpl: (handle: ReturnType<typeof setTimeout>) => void;

    private ws: GatewayWebSocket | null = null;
    private accessToken = '';
    private sessionId: string | null = null;
    private lastSeq: number | null = null;
    private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
    private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private backoffIndex = 0;
    private stopped = false;

    constructor(deps: QQGatewayClientDeps) {
        this.botName = deps.botName;
        this.getAccessToken = deps.getAccessToken;
        this.getGatewayUrl = deps.getGatewayUrl;
        this.wsFactory = deps.wsFactory;
        this.forwardInbound = deps.forwardInbound;
        this.log = deps.log;
        this.intents = deps.intents ?? DEFAULT_INTENTS;
        this.normalize = deps.normalize ?? normalizeQQEvent;
        this.backoffMs = deps.backoffMs ?? DEFAULT_BACKOFF_MS;
        this.setIntervalImpl = deps.setIntervalImpl ?? ((cb, ms) => setInterval(cb, ms));
        this.clearIntervalImpl = deps.clearIntervalImpl ?? ((h) => clearInterval(h));
        this.setTimeoutImpl = deps.setTimeoutImpl ?? ((cb, ms) => setTimeout(cb, ms));
        this.clearTimeoutImpl = deps.clearTimeoutImpl ?? ((h) => clearTimeout(h));
    }

    /** 起连接（fire-and-forget）。失败走退避重连。 */
    start(): void {
        void this.connect();
    }

    /** 停连接：关 socket、清定时器、禁止再重连。 */
    stop(): void {
        this.stopped = true;
        this.stopHeartbeat();
        if (this.reconnectTimer != null) {
            this.clearTimeoutImpl(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        try {
            this.ws?.close();
        } catch {
            /* ignore */
        }
        this.ws = null;
    }

    /** 建立一次连接：取 token + gateway url，建 ws，挂事件处理。失败则退避重连。 */
    async connect(): Promise<void> {
        if (this.stopped) return;
        try {
            this.accessToken = await this.getAccessToken();
            const url = await this.getGatewayUrl();
            const ws = this.wsFactory(url);
            this.ws = ws;
            ws.onopen = () => this.log.info(`[qq-gateway] ws connected bot=${this.botName}`);
            ws.onmessage = (ev) => this.handleMessage(typeof ev.data === 'string' ? ev.data : String(ev.data));
            ws.onerror = () => this.log.warn(`[qq-gateway] ws error bot=${this.botName}`);
            ws.onclose = () => this.handleClose();
        } catch (err) {
            this.log.error(`[qq-gateway] connect failed: ${err instanceof Error ? err.message : String(err)}`);
            this.scheduleReconnect();
        }
    }

    private async handleMessage(raw: string): Promise<void> {
        let payload: GatewayPayload;
        try {
            payload = JSON.parse(raw);
        } catch {
            this.log.error(`[qq-gateway] ws frame not JSON: ${raw.slice(0, 200)}`);
            return;
        }
        if (typeof payload.s === 'number') this.lastSeq = payload.s;

        switch (payload.op) {
            case OP_HELLO:
                this.onHello(payload.d);
                break;
            case OP_DISPATCH:
                await this.onDispatch(payload.t ?? '', payload.d);
                break;
            case OP_HEARTBEAT_ACK:
                break;
            case OP_RECONNECT:
                this.log.warn(`[qq-gateway] op:7 reconnect requested`);
                this.closeForReconnect();
                break;
            case OP_INVALID_SESSION:
                this.log.warn(`[qq-gateway] op:9 invalid session, re-identifying`);
                this.sessionId = null;
                this.closeForReconnect();
                break;
            default:
                break;
        }
    }

    private onHello(d: unknown): void {
        const interval = (d as { heartbeat_interval?: number } | undefined)?.heartbeat_interval;
        this.ws?.send(
            JSON.stringify({
                op: OP_IDENTIFY,
                d: { token: `QQBot ${this.accessToken}`, intents: this.intents, shard: [0, 1] },
            }),
        );
        if (typeof interval === 'number' && interval > 0) {
            this.startHeartbeat(interval);
        }
    }

    private async onDispatch(t: string, d: unknown): Promise<void> {
        if (t === 'READY') {
            this.sessionId = (d as { session_id?: string } | undefined)?.session_id ?? null;
            this.backoffIndex = 0; // 连上即重置退避
            this.log.info(`[qq-gateway] READY session=${this.sessionId ?? '<none>'}`);
            return;
        }
        if (t === 'RESUMED') return;

        try {
            const msg = this.normalize(t, d, { botName: this.botName });
            if (!msg) return; // 系统事件 / 未支持类型，不转发
            await this.forwardInbound(msg);
        } catch (err) {
            this.log.error(`[qq-gateway] dispatch ${t} failed: ${err instanceof Error ? err.message : String(err)}`);
        }
    }

    private startHeartbeat(ms: number): void {
        this.stopHeartbeat();
        this.heartbeatTimer = this.setIntervalImpl(() => {
            this.ws?.send(JSON.stringify({ op: OP_HEARTBEAT, d: this.lastSeq }));
        }, ms);
    }

    private stopHeartbeat(): void {
        if (this.heartbeatTimer != null) {
            this.clearIntervalImpl(this.heartbeatTimer);
            this.heartbeatTimer = null;
        }
    }

    /** 主动断开以触发重连：onclose 会接手 scheduleReconnect。 */
    private closeForReconnect(): void {
        this.stopHeartbeat();
        try {
            this.ws?.close();
        } catch {
            /* ignore */
        }
    }

    private handleClose(): void {
        this.stopHeartbeat();
        this.ws = null;
        if (this.stopped) return;
        this.scheduleReconnect();
    }

    private scheduleReconnect(): void {
        if (this.stopped) return;
        if (this.reconnectTimer != null) return; // 已排程
        const delay = this.backoffMs[Math.min(this.backoffIndex, this.backoffMs.length - 1)]!;
        this.backoffIndex++;
        this.log.warn(`[qq-gateway] reconnecting in ${delay}ms`);
        this.reconnectTimer = this.setTimeoutImpl(() => {
            this.reconnectTimer = null;
            return this.connect();
        }, delay);
    }
}
