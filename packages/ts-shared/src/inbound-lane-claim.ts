// 泳道信封的幂等协议：**原子占位**。
//
// 一条已经被判定"该由某条泳道处理"的入站信封，可能被不止一个消费者看到 —— 分区迁移期
// 间它同时躺在共享队列和分区队列上，滚动发布期间新旧 Pod 又同时在消费。谁真正处理它，
// 由 redis 上的一次原子占位决定：
//
//   claim   SET key "in-flight" EX 300 NX  → 拿到就是自己的，没拿到再看 GET
//   done    SET key "done" EX 86400        → 处理成功，长期完成标记
//   release DEL key                        → 处理失败，立刻让重投能重来
//
// ## 为什么必须原子
//
// 「先 EXISTS 看有没有处理过 → 处理 → 再标记」是三步，两个 Pod 能同时穿过第一步、各执行
// 一遍副作用（各回一条消息）。SET NX 是一次往返，赢家只有一个。
//
// ## 为什么在共享包里
//
// channel-server 和 lark-service 读写的是同一批 key。key 的格式、写下的值、TTL 差一个
// 字节，就不再是同一把锁：
//   - 完成标记写成别的值（比如 '1'），对面读到会判成"有人正在处理"，于是一直退回队列，
//     直到 24h TTL 过期后**重新处理一遍**；
//   - 占位标记被对面当成"已处理"，那条消息会被直接 ACK 掉 —— **真丢**。
// 两个 app 是两个包，编译期对不上，各写一份就只能靠人去核对。所以协议只留这一份实现，
// 两边都从这里 import。
//
// 本模块**不碰信封**：从信封的哪个字段读出 channel（缺 channel 的老信封算谁的、还是直接
// 拒绝）是各服务自己的策略，两边本来就不一样。这里只认已经解析好的四段。

import { getRedisClient } from './cache';

/** key 的第一段。队列名也用这个词，但两者是各自独立的契约，别互相引用。 */
const KEY_PREFIX = 'inbound_lane';

/**
 * 一次入站处理的身份：哪个渠道、哪个事件、哪条消息、哪条泳道。
 *
 * 四段缺一不可：少了 channel，飞书和 QQ 的同名事件互相顶掉对方的完成标记；少了 lane，
 * 同一条消息在两条泳道上只会被处理一次。
 */
export interface InboundLaneClaimKeyParts {
    channel: string;
    eventType: string;
    globalMessageId: string;
    lane: string;
}

/**
 * 幂等 key。
 *
 * **不含队列名**，这是刻意的：分区迁移期间同一条信封可能从共享队列来、也可能从分区队列
 * 来，key 认队列的话两条各算一次，用户看到两条回复。所以这个函数根本收不到队列。
 */
export function inboundLaneClaimKey(parts: InboundLaneClaimKeyParts): string {
    return [
        KEY_PREFIX,
        parts.channel,
        parts.eventType,
        parts.globalMessageId,
        parts.lane,
    ].join(':');
}

/**
 * 认领一条消息的结果。
 *   claimed    占到了，可以开始处理
 *   in-flight  别人正在处理（或者上一个持有者崩了、租约还没到期）
 *   done       已经处理完了
 */
export type LaneClaim = 'claimed' | 'in-flight' | 'done';

/** 一条信封的认领状态，由 redis 上的原子占位裁决。 */
export interface InboundLaneClaims {
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

/** 协议用到的 redis 表面，就这四个命令。 */
export interface ClaimRedis {
    setNx(key: string, value: string, seconds?: number): Promise<'OK' | null>;
    get(key: string): Promise<string | null>;
    setWithExpire(key: string, value: string, seconds: number): Promise<'OK'>;
    del(...keys: string[]): Promise<number>;
}

export function createInboundLaneClaims(redis: () => ClaimRedis): InboundLaneClaims {
    return {
        claim: async (key) => {
            // SET key value EX ttl NX —— 一次往返，原子。
            const won = await redis().setNx(key, CLAIM_IN_FLIGHT, CLAIM_LEASE_SECONDS);
            if (won !== null) return 'claimed';
            // 没占到：要分清"已经做完了"和"有人正在做"。做完了可以安心 ACK；正在做的绝
            // 不能 ACK —— 对方还没写完成标记，ACK 会把消息销毁。
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

export const inboundLaneClaims = createInboundLaneClaims(() => getRedisClient());
