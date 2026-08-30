// 同一条飞书消息在整个集群里一次只有一个处理者。
//
// 为什么需要：一个群里可以有多个我们自己的 bot，飞书会把同一条消息推给每一个。
// 不串行的话，每个 bot 都会走一遍投影 —— 各自读到"lark_message 里还没有这条"、
// 各铸一个 id、抢着写。锁是 Redis 的（跨进程），竞争者本来就在不同的 Pod 上。
//
// ## 这把锁不是正确性的最后一道防线
//
// 分布式锁做不到真互斥：持有者卡住超过租约、或者时钟漂移，就会出现两个活跃持有
// 者，而"比对 token 再删"只能防误删、维持不了互斥（真互斥要 fencing token，那要求
// 下游写入能拒绝过期的持有者，本项目的 common_* 没有这个字段，也不许改 schema）。
//
// 所以正确性由**下游写入本身**保证：身份与会话按自然键先到先得（见 tables.ts 的
// claimCommonUserId / claimCommonConversationId），消息按 om_id 收敛。锁的职责因此
// 降级成"省掉白干的活、避开必然失败的那条 already-maps-to 错误路径"。
//
// ## 等多久才放弃
//
// 等待者手上只有一个信号：GET 出来的持有者 token。它看不见续租 —— 续租改的是 TTL、
// 不是值。于是"锁还在、token 没变"这一个观察底下压着两个世界：持有者活着一直在续，
// 或者持有者早崩了、只是最后那次续租留下的 TTL 还没走完。所以窗口不能按"进场那一刻
// + 一个常量"算：续租能把锁的残留寿命推到任意远，固定窗口跟它之间没有任何关系。
//
// 从 GET 得到的观察出发，时间轴上能推的只有这两条：
//   * 在 t0 看到 token T。持有者若在 t0 之前就崩了、之后没再续，锁最迟 t0+租约 过期。
//   * 到了 t0+租约 token 还是 T，那这中间必然有过一次续租 —— 持有者那时是活的。
//     那次续租最晚发生在 t0+租约，它留下的锁最迟 t0+2*租约 过期。
//
// 于是等到 t0 + 2*租约 + 余量：崩在"进场后第一次证明自己还活着"那一下的持有者，
// 它留下的锁到这时一定已经过期，等待者接得住。再往后 token 仍是 T，说明它至少续了
// 两轮 —— 那不是崩溃残留，是一个真在干活的长任务，这时候放弃才是对的，接着等只是把
// 交接那次 HTTP 一直挂着（投递方在 LANE_HANDOFF_TIMEOUT_MS 里干等，见
// ingress/lane-handoff.ts）。窗口因此**严格大于**租约（这条不等式有测试钉着）：
// 反过来（旧实现是租约 120s、等待 60s）会在持有者崩溃时丢消息 —— 崩溃留下的锁要等
// 满租约才过期，同批到达的其他 bot 在那之前就全散了，而飞书入口早已 ACK。
//
// t0 是**最后一次看到锁动了**的时刻，不是进场时刻：token 变了（易主）或者锁没了，
// 都说明系统在推进，凭什么让排在后面的人在队伍还在动的时候放弃。代价是排队的人总
// 等待可能超过一个窗口 —— 同一条消息的竞争者只有群里那几个 bot，队列本来就短。
//
// 租约取 30s 而不是更长：租约越长，崩溃后的空转越久。盖不住的长任务由续租负责。

/** 本模块用到的 Redis 表面，就这三个命令。 */
export interface MessageLockRedis {
    get(key: string): Promise<string | null>;
    setNx(key: string, value: string, seconds?: number): Promise<'OK' | null>;
    evalScript(
        script: string,
        numKeys: number,
        ...keysAndArgs: (string | number)[]
    ): Promise<unknown>;
}

/** 锁在存储侧的动作。前三个都必须是原子的（先查再写会让两个 Pod 一起穿过）。 */
export interface MessageLockStore {
    /** 抢锁。抢到返回 true。 */
    acquire(key: string, token: string, leaseSeconds: number): Promise<boolean>;
    /** 续租。**只在自己仍持有时才续**；返回 false 说明锁已经是别人的了。 */
    renew(key: string, token: string, leaseSeconds: number): Promise<boolean>;
    /** 还锁。只删自己那把。 */
    release(key: string, token: string): Promise<void>;
    /**
     * 现在谁持有；没人持有是 null。**只用来看"锁还动不动"**，不参与任何决定 ——
     * 读到的那一刻它就可能过期了，所以这一个不需要原子性。
     */
    holder(key: string): Promise<string | null>;
}

export interface LockHeartbeat {
    stop(): void;
}

/** 续租的节拍器。注入的原因只有一个：让"任务跑了三个租约那么久"能确定性地演。 */
export type ScheduleHeartbeat = (everyMs: number, tick: () => void) => LockHeartbeat;

export type LarkMessageLock = <T>(omId: string, run: () => Promise<T>) => Promise<T>;

const LEASE_SECONDS = 30;

/**
 * 观察窗口在两个租约之外多给的余量。等待者是隔一段轮询一次、不是订阅，Redis 判过期
 * 用的又是它自己的钟：半个租约足够盖住轮询间隔、时钟漂移和一次续租的往返。
 */
const STALE_GRACE_MS = 15_000;

export const LARK_MESSAGE_LOCK = {
    /** 租约。持有者崩掉之后，这条消息要等这么久才能被别人接手。 */
    leaseSeconds: LEASE_SECONDS,
    /** 续租间隔。要留出重试余量，所以取租约的三分之一。 */
    renewEveryMs: 10_000,
    /**
     * 锁一直捏在同一个人手里能等多久 —— 从**最后一次看到锁动了**算起，不是从进场
     * 算起。两个租约 + 余量，推导见文件顶部。**必须大于租约**。
     */
    waitTimeoutMs: 2 * LEASE_SECONDS * 1000 + STALE_GRACE_MS,
    retryEveryMs: 25,
} as const;

const RENEW_SCRIPT = `
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
`;

const RELEASE_SCRIPT = `
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
`;

/**
 * Redis 实现。每个动作各自一次往返：
 *   acquire  SET key token EX ttl NX
 *   renew    Lua：GET 比对 token 再 EXPIRE
 *   release  Lua：GET 比对 token 再 DEL
 *   holder   GET
 *
 * 两个 Lua 都必须先比对：无条件 EXPIRE 会替别人把锁续上，无条件 DEL 会把别人的锁
 * 删掉 —— 后者正是这把锁要防的事。
 */
export function redisMessageLockStore(redis: () => MessageLockRedis): MessageLockStore {
    return {
        acquire: async (key, token, leaseSeconds) =>
            (await redis().setNx(key, token, leaseSeconds)) === 'OK',
        renew: async (key, token, leaseSeconds) =>
            Number(await redis().evalScript(RENEW_SCRIPT, 1, key, token, leaseSeconds)) === 1,
        release: async (key, token) => {
            await redis().evalScript(RELEASE_SCRIPT, 1, key, token);
        },
        holder: (key) => redis().get(key),
    };
}

export interface MessageLockOptions {
    sleep?: (ms: number) => Promise<void>;
    now?: () => number;
    newToken?: () => string;
    schedule?: ScheduleHeartbeat;
}

const realSchedule: ScheduleHeartbeat = (everyMs, tick) => {
    const timer = setInterval(tick, everyMs);
    // 心跳不该把进程钉在事件循环上：任务跑完之前进程要能正常退出。
    (timer as unknown as { unref?: () => void }).unref?.();
    return { stop: () => clearInterval(timer) };
};

export function larkMessageLock(
    store: MessageLockStore,
    options: MessageLockOptions = {},
): LarkMessageLock {
    const sleep = options.sleep ?? ((ms: number) => Bun.sleep(ms));
    const now = options.now ?? Date.now;
    const newToken = options.newToken ?? (() => Bun.randomUUIDv7());
    const schedule = options.schedule ?? realSchedule;
    const { leaseSeconds, renewEveryMs, waitTimeoutMs, retryEveryMs } = LARK_MESSAGE_LOCK;

    return async function withLarkMessageLock<T>(omId: string, run: () => Promise<T>): Promise<T> {
        const key = `lock:lark:message-projection:${omId}`;
        const token = newToken();
        const startedAt = now();
        // 上一轮看到的持有者。undefined 是"还没看过"，跟"看过、当时没人持有"（null）
        // 不是一回事 —— 后者是一次真的观察，会让窗口重新起算。
        let seenHolder: string | null | undefined;
        let deadline = now() + waitTimeoutMs;

        for (;;) {
            if (await store.acquire(key, token, leaseSeconds)) break;
            // 抢不到才多花一次往返去看是谁。锁一动（易主 / 没了）就说明系统在推进，
            // 窗口重新起算，理由见文件顶部。
            const holder = await store.holder(key);
            if (holder !== seenHolder) {
                seenHolder = holder;
                deadline = now() + waitTimeoutMs;
            }
            if (now() >= deadline) {
                // 抛出去而不是静默跳过。这条消息没有自动重试可指望：泳道交接只会
                // 变成非 2xx、投递方不再送第二次，飞书直连的两个入口早就 ACK 过了。
                // 留下的只有这条错误，所以它必须说清楚是哪条消息、等了多久。
                throw new Error(
                    `timeout acquiring the lark message projection lock for ${omId} ` +
                        `after ${now() - startedAt}ms; one handler has been holding it for ` +
                        `the whole ${waitTimeoutMs}ms window without the lock moving`,
                );
            }
            await sleep(retryEveryMs);
        }

        let heartbeat: LockHeartbeat | undefined;
        heartbeat = schedule(renewEveryMs, () => {
            void store
                .renew(key, token, leaseSeconds)
                .then((stillMine) => {
                    if (stillMine) return;
                    // 锁已经不是我们的了，互斥在这一刻已经破了。中途掐掉一个正在写库
                    // 的任务比让它跑完更糟（写了一半），所以继续跑 —— 但要吼出来，
                    // 否则这件事完全不可观测。落库的收敛靠自然键，不靠这把锁。
                    heartbeat?.stop();
                    console.error(
                        `[lark-projection] lost the projection lease for ${omId} while still ` +
                            'working; another handler may be projecting the same message',
                    );
                })
                .catch((error) => {
                    console.warn(`[lark-projection] failed to renew the lease for ${omId}:`, error);
                });
        });

        try {
            return await run();
        } finally {
            heartbeat.stop();
            // 还锁失败不该覆盖任务本身的结果。有租约兜底，最坏是这条消息多等一个
            // 租约周期。
            try {
                await store.release(key, token);
            } catch (error) {
                console.warn(
                    `[lark-projection] failed to release the projection lock for ${omId}:`,
                    error,
                );
            }
        }
    };
}
