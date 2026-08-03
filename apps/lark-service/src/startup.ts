/**
 * 启动与关停的顺序。真实的单例（DataSource / botDirectory / rabbitmqClient /
 * Redis）在 index.ts 里装配后递进来，这里只负责"先做什么后做什么、哪一步失败要
 * 拦住"，所以顺序本身是可测的。
 */

import type { BotLoadOptions } from '@inner/shared/bot';

/**
 * 本服务负责的 bot 范围。**必须显式传给 BotDirectory.load**：
 * 不传 = 加载全渠道（channel-server 的用法），传空数组 BotDirectory 会 fail-closed
 * 抛错。飞书进程持有 QQ 的 credentials 就等于拆服务白拆。
 */
export const LARK_BOT_SCOPE: BotLoadOptions = { channels: ['lark'] };

export interface LarkBackends {
    readonly database: {
        initialize(): Promise<unknown>;
        destroy(): Promise<unknown>;
        readonly isInitialized: boolean;
    };
    readonly bots: {
        load(options: BotLoadOptions): Promise<void>;
    };
    readonly cache: {
        ping(): Promise<unknown>;
        close(): Promise<unknown>;
    };
    readonly broker: {
        connect(): Promise<void>;
        declareTopology(): Promise<void>;
        close(): Promise<void>;
    };
    /** 飞书原始报文的审计落库。旁路，但连不上要在启动期就知道。 */
    readonly eventLog: {
        open(): Promise<void>;
        close(): Promise<void>;
    };
}

/**
 * 顺序不是随便排的：bot 目录要读 bot_config / common_user，所以库先连；Redis 和
 * MQ 只在这里做一次可达性确认，让配错的 env 在 Pod 起来那一刻就 crash，而不是等
 * 第一条飞书消息进来才暴露。任何一步失败都直接抛出去 —— 半连着的进程会通过健康
 * 检查、然后静默丢消息。
 */
export async function bootLarkService(backends: LarkBackends): Promise<void> {
    // 逐步打日志：卡在连库和卡在连 MQ 从外面看是同一个现象（Pod 起不来、没日志），
    // 只有走到哪一步的记号能区分。
    await backends.database.initialize();
    console.info('[lark-service] postgres connected');
    await backends.bots.load(LARK_BOT_SCOPE);
    console.info('[lark-service] lark bot directory loaded');
    await backends.cache.ping();
    console.info('[lark-service] redis reachable');
    await backends.broker.connect();
    await backends.broker.declareTopology();
    console.info('[lark-service] rabbitmq topology declared');
    // 审计是入站的旁路（记不上不挡消息），但"连不上"要在这里暴露：等到第一条飞书
    // 消息进来才发现 mongo 配错了，那时每条事件都会刷一行错误日志。
    await backends.eventLog.open();
    console.info('[lark-service] event log ready');
}

/**
 * 关停顺序与启动相反：先停消费/发布（MQ），再关它依赖的存储。
 * 每一步各自兜异常 —— 一步抛错就跳过其余关闭的话，会留下没 quit 的连接，进程
 * exit 之后在服务端侧变成僵死连接。
 */
export async function shutdownLarkService(backends: LarkBackends): Promise<void> {
    await closeQuietly('rabbitmq', () => backends.broker.close());
    await closeQuietly('event log', () => backends.eventLog.close());
    await closeQuietly('redis', () => backends.cache.close());
    if (backends.database.isInitialized) {
        await closeQuietly('postgres', () => backends.database.destroy());
    }
}

async function closeQuietly(name: string, close: () => Promise<unknown>): Promise<void> {
    try {
        await close();
    } catch (error) {
        console.warn(`[lark-service] failed to close ${name}:`, error);
    }
}
