/**
 * 被动回复窗口管理器（网关独占）。
 *
 * 把纯决策（decision.ts）接上持久化存储（Redis / 内存）与进程内串行化：
 *   - reserve() 给一条出站消息分配 msg_seq 或决定丢弃；
 *   - 同一 (botName, msgId) 用 per-key mutex 串行，避免并发多段回复抢到同一个 seq；
 *   - 窗口记录与幂等键落存储，重启后状态延续。
 *
 * 实际调 QQ api 发消息由调用方做：reserve 只负责「能不能发、用哪个 seq」。
 * reserve 返回 send 即已消耗 seq + 记幂等；后续真正发送失败不重试（被动窗口best-effort）。
 */

import { Mutex } from 'async-mutex';
import { decidePassiveReply, type DropReason, type WindowRecord } from './decision';

export interface PassiveWindowStore {
    /** SET NX 语义：首次写入返回 true，键已存在返回 false。 */
    markIdempotent(key: string, ttlMs: number): Promise<boolean>;
    getWindow(key: string): Promise<WindowRecord | null>;
    setWindow(key: string, rec: WindowRecord, ttlMs: number): Promise<void>;
}

export interface ReserveParams {
    botName: string;
    /** 被动回复所回应的原始 QQ msg_id；缺失即视为主动发，丢弃。 */
    replyToMessageId?: string;
    idempotencyKey: string;
}

export type ReserveResult =
    | { action: 'send'; msgSeq: number }
    | { action: 'drop'; reason: DropReason };

export interface PassiveWindowOptions {
    windowMs?: number;
    maxReplies?: number;
    now?: () => number;
}

const DEFAULT_WINDOW_MS = 60 * 60 * 1000; // 60min
const DEFAULT_MAX_REPLIES = 4;

export class PassiveWindowManager {
    private readonly store: PassiveWindowStore;
    private readonly windowMs: number;
    private readonly maxReplies: number;
    private readonly now: () => number;
    /** per-key 进程内锁，串行同一 msgId 的并发 reserve。 */
    private readonly locks = new Map<string, Mutex>();

    constructor(store: PassiveWindowStore, opts: PassiveWindowOptions = {}) {
        this.store = store;
        this.windowMs = opts.windowMs ?? DEFAULT_WINDOW_MS;
        this.maxReplies = opts.maxReplies ?? DEFAULT_MAX_REPLIES;
        this.now = opts.now ?? (() => Date.now());
    }

    async reserve(params: ReserveParams): Promise<ReserveResult> {
        // 【单副本约束】reserve 用进程内 mutex 串行 + Redis 非原子读改写（markIdempotent /
        // getWindow / setWindow 分离调用）。这只在 qq-gateway **单副本部署**下正确：本期
        // QQ webhook 单入口、量小，单副本足够。多副本会跨 pod 重复 msg_seq、漏计 4 次上限
        // ——届时必须把 reserve 改成 Redis Lua 原子化（窗口读判递增写一次完成），不能再依赖
        // 进程内锁。改部署副本数前先改这里。
        // 主动发不碰存储、不记幂等：直接丢弃
        if (!params.replyToMessageId) {
            return { action: 'drop', reason: 'active_send' };
        }

        const windowKey = `${params.botName}:${params.replyToMessageId}`;
        const lock = this.lockFor(windowKey);
        return lock.runExclusive(async () => {
            // 幂等：NX 写入，已存在即重投
            const fresh = await this.store.markIdempotent(this.idemKey(params.idempotencyKey), this.windowMs);
            const record = await this.store.getWindow(this.winKey(windowKey));

            const decision = decidePassiveReply({
                hasReplyTo: true,
                idempotencyAlreadySeen: !fresh,
                record,
                now: this.now(),
                windowMs: this.windowMs,
                maxReplies: this.maxReplies,
            });

            if (decision.action === 'send') {
                // 窗口记录留存 2 倍窗口长度，保证过期判定能命中（而非键消失后误判为新窗口）
                await this.store.setWindow(this.winKey(windowKey), decision.nextRecord, this.windowMs * 2);
                return { action: 'send', msgSeq: decision.msgSeq };
            }
            return { action: 'drop', reason: decision.reason };
        });
    }

    private lockFor(key: string): Mutex {
        let m = this.locks.get(key);
        if (!m) {
            m = new Mutex();
            this.locks.set(key, m);
        }
        return m;
    }

    private winKey(key: string): string {
        return `qqwin:${key}`;
    }

    private idemKey(key: string): string {
        return `qqidem:${key}`;
    }
}

/**
 * 内存版存储：用于测试与单实例兜底。不感知 TTL 过期（窗口过期由 decision 的 windowStart 判定，
 * 不依赖存储删键），重启即丢失。生产用 RedisPassiveWindowStore。
 */
export class InMemoryPassiveWindowStore implements PassiveWindowStore {
    private readonly idem = new Set<string>();
    private readonly windows = new Map<string, WindowRecord>();

    async markIdempotent(key: string, _ttlMs: number): Promise<boolean> {
        if (this.idem.has(key)) return false;
        this.idem.add(key);
        return true;
    }

    async getWindow(key: string): Promise<WindowRecord | null> {
        return this.windows.get(key) ?? null;
    }

    async setWindow(key: string, rec: WindowRecord, _ttlMs: number): Promise<void> {
        this.windows.set(key, { ...rec });
    }
}
