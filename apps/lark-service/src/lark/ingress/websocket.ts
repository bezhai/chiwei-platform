// 入口二：飞书长连。SDK 主动向飞书建一条 WebSocket，事件从连接上推过来。
//
// 这是三个入口里唯一**主动**的一个，也是唯一一个会互相抢的：飞书对同一个 app_id
// 的多个长连客户端是**随机投递**而不是广播。两个进程同时连着，线上事件就被静默分
// 走一半 —— prod 不断流、错误率不涨、告警抓不到，只有用户觉得"她有时候不理我"。
// 所以谁持有长连必须是全局唯一的，见 holdsLarkWebSockets。

import { LoggerLevel, WSClient, type EventDispatcher } from '@larksuiteoapi/node-sdk';
import { isProdDeployment } from '@inner/shared/lane-policy';

import type { LarkCredentials } from '../credentials';
import { createEventDispatcher } from './event-dispatcher';
import type { LarkEventSink } from './event-sink';

/**
 * 一条长连的真实状态。
 *   connecting    还没连上（首次连接中，或正在重连）
 *   connected     连上了，事件会推过来
 *   disconnected  连不上 / 已关闭
 */
export type LarkWebSocketState = 'connecting' | 'connected' | 'disconnected';

export interface LarkWebSocketClient {
    start(params: { eventDispatcher: EventDispatcher }): Promise<void>;
    close(params?: { force?: boolean }): void;
    /**
     * 真实连接状态。**必须问底层的 socket**，不能用"start() 返回了"代替 —— 见下方
     * realClient 的注释。
     */
    state(): LarkWebSocketState;
}

export type LarkWebSocketClientFactory = (credentials: LarkCredentials) => LarkWebSocketClient;

export interface LarkWebSocketBot {
    botName: string;
    credentials: LarkCredentials;
    sink: LarkEventSink;
}

const LARK_DIRECT_INGRESS = 'LARK_DIRECT_INGRESS';

/**
 * 本进程该不该持有长连。两个条件同时成立才行：
 *
 *   1. **是 prod 部署**。硬约束，没有 env 后门 —— LARK_DIRECT_INGRESS 挂在 app env
 *      上、所有泳道天然继承，只靠 env 挡不住泳道建连。
 *   2. **开关打开**。语义是"prod 里入站走不走长连"的回退开关：webhook 是被动路由，
 *      进程起来就注册；长连是主动的，要显式打开。
 *
 * 两个都是**部署属性**（这个进程要不要当飞书入口），所以读环境变量（部署期决定）
 * 而不是运行时配置。
 */
export function holdsLarkWebSockets(): boolean {
    return isProdDeployment() && process.env[LARK_DIRECT_INGRESS] === 'true';
}

/** WebSocket.readyState 的 OPEN。标准值，不必为了一个常量把 ws 包拉进来。 */
const WEBSOCKET_OPEN = 1;

/**
 * 真实的 SDK 客户端。
 *
 * **SDK 的 `start()` 只是调 `reConnect(true)` 就返回，不等首次连接成功。** 所以
 * `await start()` 回来之后进程很可能一条飞书事件都收不到。就绪状态只能问底层
 * socket 的 readyState —— 这是从 SDK 源码里确认过的唯一真实信号（`wsConfig`
 * 在连接成功的 'open' 回调里才被 setWSInstance，断开/关闭时置回 null）。
 *
 * 这一条直接关系到切流：如果判据是"新服务健康检查绿了"就停旧的，而绿灯来自
 * "start() 返回了"，那就会在没人接飞书消息的情况下把旧入口停掉，且无告警。
 */
const realClient: LarkWebSocketClientFactory = (credentials) => {
    const client = new WSClient({
        appId: credentials.app_id,
        appSecret: credentials.app_secret,
        loggerLevel: LoggerLevel.info,
    });

    return {
        start: (params) => client.start(params),
        close: (params) => client.close(params),
        state: () => {
            const socket = (
                client as unknown as {
                    wsConfig: { getWSInstance(): { readyState?: number } | null };
                }
            ).wsConfig.getWSInstance();
            // 没有 socket = 还没连上或正在重连。SDK 默认 autoReconnect，所以这是
            // 「还在努力」而不是「放弃了」。
            if (!socket) return 'connecting';
            return socket.readyState === WEBSOCKET_OPEN ? 'connected' : 'disconnected';
        },
    };
};

/** 本进程长连入口的就绪情况。切流判据读这个，不读"启动函数有没有返回"。 */
export interface LarkWebSocketStatus {
    /** 本进程期望持有的长连数。不持有长连的部署是 0。 */
    expected: number;
    /** 真正连上的。 */
    connected: number;
    bots: Array<{ botName: string; state: LarkWebSocketState }>;
}

export interface LarkWebSockets {
    close(): void;
    status(): LarkWebSocketStatus;
}

/**
 * 给每个 bot 各开一条长连。
 *
 * **不等连上就返回。** 连接是异步的、而且断了会自动重连，所以"连上了没有"是一个
 * 持续变化的状态，不是启动时的一次性结果 —— 用 status() 问，别拿本函数返回当就绪。
 *
 * 一个 bot 连不上（凭据过期、应用被停用）只记日志、不拖垮其他 bot：几个飞书应用之间
 * 没有依赖关系。但它仍然算在 expected 里 —— 少一个 bot 就是少一个 bot 的消息没人接，
 * 不能因为它失败了就把它从分母里抹掉。
 */
export async function openLarkWebSockets(
    bots: readonly LarkWebSocketBot[],
    createClient: LarkWebSocketClientFactory = realClient,
): Promise<LarkWebSockets> {
    const opened: Array<{ botName: string; client: LarkWebSocketClient | null }> = [];
    let closed = false;

    await Promise.all(
        bots.map(async (bot) => {
            try {
                const client = createClient(bot.credentials);
                await client.start({
                    eventDispatcher: createEventDispatcher(bot.credentials, bot.sink),
                });
                opened.push({ botName: bot.botName, client });
                console.info(
                    `[lark-ingress] websocket starting: ${bot.botName} ` +
                        `(${bot.credentials.app_id}) — not connected yet, watch readiness`,
                );
            } catch (error) {
                opened.push({ botName: bot.botName, client: null });
                console.error(
                    `[lark-ingress] websocket failed: ${bot.botName} ` +
                        `(${bot.credentials.app_id}):`,
                    error,
                );
            }
        }),
    );

    const stateOf = (entry: (typeof opened)[number]): LarkWebSocketState => {
        if (closed || !entry.client) return 'disconnected';
        return entry.client.state();
    };

    return {
        close() {
            // 关过就不再关第二次。名单留着 —— status() 关停后仍要说得出本来该有几条
            // 连接，否则"下掉之后就绪也是绿的"又是一个假信号。
            if (closed) return;
            closed = true;
            for (const entry of opened) {
                entry.client?.close({ force: true });
            }
        },
        status() {
            const botStates = opened.map((entry) => ({
                botName: entry.botName,
                state: stateOf(entry),
            }));
            return {
                expected: botStates.length,
                connected: botStates.filter((b) => b.state === 'connected').length,
                bots: botStates,
            };
        },
    };
}
