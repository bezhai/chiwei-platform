// 入站信封的幂等协议：**原子占位**。
//
// ⚠️ 这是**跨服务契约**。双订阅窗口里同一条信封可能被 channel-server 拿到、也可能被
// lark-service 拿到，两边读写的是 redis 里同一批 key（key 的格式见 inbound-lane.ts 的
// inboundDedupeKey）。所以统一 key 的格式不够，值和状态机也要一样：
//
//   claim   SET key "in-flight" EX 300 NX  → 拿到就是自己的，没拿到再看 GET
//   done    SET key "done" EX 86400        → 处理成功，长期完成标记
//   release DEL key                        → 处理失败，立刻让重投能重来
//
// 两边不一致的后果不是"多处理一次"这种小事：
//   - 完成标记写成别的值（比如 '1'），对面读到会判成"有人正在处理"，于是一直退回队列，
//     直到 24h TTL 过期后**重新处理一遍**；
//   - 占位标记被对面当成"已处理"，那条消息会被直接 ACK 掉——**真丢**。
//
// 对应的实现在 apps/lark-service/src/lark/ingress/lane-queue.ts，两边各钉一条字面量
// 断言（两个 app 是两个包，编译期对不上）。
//
// ## 为什么必须原子
//
// 「先 EXISTS 看有没有处理过 → 处理 → 再标记」是三步，两个 Pod 能同时穿过第一步、各
// 执行一遍副作用（各回一条消息）。SET NX 是一次往返，赢家只有一个。

import { getRedisClient } from '@inner/shared/cache';

/**
 * 认领一条消息的结果。
 *   claimed    占到了，可以开始处理
 *   in-flight  别人正在处理（或者上一个持有者崩了、租约还没到期）
 *   done       已经处理完了
 */
export type LaneClaim = 'claimed' | 'in-flight' | 'done';

export interface InboundLaneStore {
    claim(key: string): Promise<LaneClaim>;
    /** 处理成功，占位转成长期的完成标记。 */
    complete(key: string): Promise<void>;
    /** 处理失败，立刻释放占位，让重投能重来。 */
    release(key: string): Promise<void>;
}

export const CLAIM_IN_FLIGHT = 'in-flight';
export const CLAIM_DONE = 'done';

/**
 * 占位租约。短，因为它回答的是"持有者崩了之后多久能被别人重新处理"；长了会让一条消息
 * 在持有者已经死掉的情况下白等。
 */
export const CLAIM_LEASE_SECONDS = 5 * 60;

/** 完成标记的存活时间要盖住 MQ 的重投窗口。 */
export const CLAIM_DONE_TTL_SECONDS = 24 * 60 * 60;

/** store 用到的 redis 表面，就这四个命令。 */
export interface LaneRedis {
    setNx(key: string, value: string, seconds?: number): Promise<'OK' | null>;
    get(key: string): Promise<string | null>;
    setWithExpire(key: string, value: string, seconds: number): Promise<'OK'>;
    del(...keys: string[]): Promise<number>;
}

export function createInboundLaneStore(redis: () => LaneRedis): InboundLaneStore {
    return {
        claim: async (key) => {
            // SET key value EX ttl NX —— 一次往返，原子。
            const won = await redis().setNx(key, CLAIM_IN_FLIGHT, CLAIM_LEASE_SECONDS);
            if (won !== null) return 'claimed';
            // 没占到：要分清"已经做完了"和"有人正在做"。做完了可以安心 ACK；正在做的绝
            // 不能 ACK——对方还没写完成标记，ACK 会把消息销毁。
            const held = await redis().get(key);
            return held === CLAIM_DONE ? 'done' : 'in-flight';
        },
        // 一条命令写值 + 续期。分成 SET 再 EXPIRE 的话，中间崩掉会留一个永不过期的 key。
        complete: async (key) => {
            await redis().setWithExpire(key, CLAIM_DONE, CLAIM_DONE_TTL_SECONDS);
        },
        release: async (key) => {
            await redis().del(key);
        },
    };
}

export const inboundLaneStore = createInboundLaneStore(() => getRedisClient());
