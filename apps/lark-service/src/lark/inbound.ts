// 飞书入站的装配根：三个入口在这里接到同一层解析上。
//
//   webhook（被动，HTTP）─┐
//   长连（主动，WS）     ─┼─▶ LarkEvent ─▶ 处理表 ─▶ 解析层 ─▶ onMessage
//   泳道队列（重放，MQ） ─┘
//
// 三条传输各有各的信封和各自的失败语义（见 ingress/ 下三个文件），但**解析只有一
// 份**：同一条飞书消息不管从哪条路进来，交出去的领域对象逐字段相同。
//
// 处理表是这里建的一个普通对象。想认领新的事件类型（群成员变化、卡片回调……）就
// 往表里加一项 —— 不需要装饰器、不需要注册表、不需要 import 顺序。

import type { Hono } from 'hono';

import type { InboundLaneClaims } from '@inner/shared/inbound-lane-claim';

import { createLarkBotLookup, type LarkBotRoster, type LarkPersonaName } from './bot-lookup';
import { LARK_CHANNEL } from './channel';
import { larkCredentials } from './credentials';
import { createLarkEventSink } from './ingress/event-sink';
import {
    deliverLarkEvent,
    UnprocessableLarkEvent,
    type LarkEvent,
    type LarkEventHandlers,
} from './ingress/lark-event';
import { startInboundLaneConsumer, type LaneChannel } from './ingress/lane-queue';
import { registerLarkWebhook } from './ingress/webhook';
import {
    openLarkWebSockets,
    type LarkWebSocketBot,
    type LarkWebSocketClientFactory,
    type LarkWebSockets,
} from './ingress/websocket';
import {
    readLarkMessageEvent,
    type LarkMessageReading,
} from './message/read-message-event';
import type { LarkMessageEvent } from './message/wire';

export interface LarkInboundPorts {
    /** 本进程负责哪些 bot。 */
    roster: LarkBotRoster;
    /** persona_id → 人设展示名，启动时读一次（见 persona-names.ts）。 */
    personaName: LarkPersonaName;
    /** 原始报文落库，只为可追溯。 */
    record: (payload: unknown) => Promise<void>;
    /** 解析完成后的去处。 */
    onMessage: (reading: LarkMessageReading, event: LarkEvent) => Promise<void>;
    /**
     * 卡片上的按钮被按下。
     *
     * **不过解析层**：卡片回调根本不是一条消息（没有 message_id、没有正文、不进
     * common_message），它带回来的只有按钮里那份 value。所以这里交出去的是原始报文，
     * 由认领它的人自己解释（见 photo/callback.ts）。
     */
    onCardAction: (payload: unknown) => Promise<void>;
}

export interface LarkInbound {
    /** 入口一：把 webhook 路由挂到 HTTP app 上。 */
    registerWebhooks(app: Hono): void;
    /**
     * 入口二：给长连模式的 bot 各开一条连接。
     *
     * 返回的句柄既能关，也能问**真实**连接状态 —— 别拿"这个 Promise resolve 了"
     * 当就绪判据，SDK 不等连上就返回。
     */
    openWebSockets(createClient?: LarkWebSocketClientFactory): Promise<LarkWebSockets>;
    /**
     * 入口三：消费本泳道的信封队列。切换期间是两条队列（分区前的共享队列 + 按 channel
     * 分区的新队列），订阅哪些见 ingress/lane-queue.ts。
     */
    consumeLane(
        lane: string,
        deps?: {
            amqp?: LaneChannel;
            claims?: InboundLaneClaims;
            wait?: (ms: number) => Promise<void>;
            channelQueueEnabled?: () => Promise<boolean>;
        },
    ): Promise<void>;
}

export function createLarkInbound(ports: LarkInboundPorts): LarkInbound {
    const bots = createLarkBotLookup(ports.roster, ports.personaName);

    const handlers: LarkEventHandlers = {
        'im.message.receive_v1': async (event) => {
            const reading = readLarkMessageEvent(event.payload as LarkMessageEvent, bots);
            if (!reading) {
                // 报文里没有 message_id：这个载荷不是一条消息，重试多少次都一样。
                // 说成"永久失败"而不是静默跳过 —— 队列那条路要靠这个区分决定丢还是
                // 重投，当成成功 ACK 掉就是又一条静默丢失。
                throw new UnprocessableLarkEvent(
                    'lark event payload carries no message id; it is not a message event',
                );
            }
            await ports.onMessage(reading, event);
        },
        // 第二条入站路径，与消息那条完全不搭界：不过解析、不过规则、不看 @。
        'card.action.trigger': (event) => ports.onCardAction(event.payload),
    };

    const deliver = (event: LarkEvent) => deliverLarkEvent(event, handlers);

    /** 飞书 SDK 那两个入口共用的收口：审计 + 立刻应答 + 异步处理。 */
    const sinkFor = (botName: string) =>
        createLarkEventSink({ botName, record: ports.record, deliver });

    const larkBots = (initType: 'http' | 'websocket') =>
        ports.roster
            .getAllBotConfigs()
            .filter((bot) => bot.channel === LARK_CHANNEL && bot.init_type === initType);

    return {
        registerWebhooks(app) {
            for (const bot of larkBots('http')) {
                registerLarkWebhook(
                    app,
                    { botName: bot.bot_name, credentials: larkCredentials(bot) },
                    sinkFor(bot.bot_name),
                );
            }
        },

        openWebSockets(createClient) {
            const wsBots: LarkWebSocketBot[] = larkBots('websocket').map((bot) => ({
                botName: bot.bot_name,
                credentials: larkCredentials(bot),
                sink: sinkFor(bot.bot_name),
            }));
            return openLarkWebSockets(wsBots, createClient);
        },

        // 泳道信封不过 sinkFor：它是重放，审计在它第一次进来时就记过了，而且这条路上
        // 失败要让 MQ 知道，不能像飞书那两个入口一样吞掉。
        //
        // scope 同时是分区维度和防线（见 lane-queue.ts 顶部）：channel + lane 决定订
        // 阅哪条分区队列，也决定共享队列上哪些信封该退回去。handles 直接取处理表的键，
        // 所以"本服务认领了哪些事件"不会跟处理表脱节。
        consumeLane(lane, deps) {
            return startInboundLaneConsumer(
                {
                    channel: LARK_CHANNEL,
                    lane,
                    handles: (eventType) => handlers[eventType] !== undefined,
                },
                deliver,
                deps,
            );
        },
    };
}
