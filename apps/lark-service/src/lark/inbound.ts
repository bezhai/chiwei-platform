// 飞书入站的装配根：三个入口在这里接到同一层解析上。
//
//   webhook（被动，HTTP）   ─┐
//   长连（主动，WS）        ─┼─▶ LarkEvent ─▶ 处理表 ─▶ 解析层 ─▶ onMessage
//   泳道信封（交接，HTTP）  ─┘
//
// 三条传输各有各的信封和各自的失败语义（见 ingress/ 下几个文件），但**解析只有一
// 份**：同一条飞书消息不管从哪条路进来，交出去的领域对象逐字段相同。
//
// 处理表是这里建的一个普通对象。想认领新的事件类型（群成员变化、卡片回调……）就
// 往表里加一项 —— 不需要装饰器、不需要注册表、不需要 import 顺序。

import type { Hono } from 'hono';

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
import { registerLarkLaneInbound } from './ingress/lane-inbound';
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
import type { LarkMessageEvent, LarkRecallEvent } from './message/wire';

export interface LarkInboundPorts {
    /** 本进程负责哪些 bot。 */
    roster: LarkBotRoster;
    /**
     * 本进程所在泳道（prod 部署是 'prod'）。
     *
     * **不参与任何路由决定** —— 交接来的信封带着自己的泳道，本进程只是执行者。它唯一
     * 的用处是在接收端点上回报"接住这次交接的是谁"，投递方据此看出交接落回了 prod。
     */
    lane: string;
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
    /**
     * 有人在飞书撤回了一条消息。
     *
     * **也不过解析层**：报文里没有发送者、没有正文，它不会在公共层建任何新行，要做的
     * 只是把已经在库里的那一行标成撤回。
     *
     * `receivedAt` 是入口应答那一刻的时间，报文不带撤回时刻时拿它兜底 —— 在这里传下去
     * 而不是由认领者现取，因为认领者跑在应答之后（见 ingress/lark-event.ts 的 receivedAt）。
     */
    onRecall: (recall: LarkRecallEvent, receivedAt: Date) => Promise<void>;
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
     * 入口三：接住 prod 交接过来的泳道信封。**每个部署都要挂**（prod 也挂）——
     * 泳道的 Service 不存在时 sidecar 会把交接打回 prod 自己，那时接住它的就是这里。
     */
    registerLaneInbound(app: Hono): void;
}

export function createLarkInbound(ports: LarkInboundPorts): LarkInbound {
    const bots = createLarkBotLookup(ports.roster, ports.personaName);

    const handlers: LarkEventHandlers = {
        'im.message.receive_v1': async (event) => {
            const reading = readLarkMessageEvent(event.payload as LarkMessageEvent, bots);
            if (!reading) {
                // 报文里没有 message_id：这个载荷不是一条消息，重发多少次都一样。
                // 抛出去而不是静默跳过 —— 交接那条路要靠它变成非 2xx，当成处理成功
                // 应答就是又一条静默丢失。
                throw new UnprocessableLarkEvent(
                    'lark event payload carries no message id; it is not a message event',
                );
            }
            await ports.onMessage(reading, event);
        },
        // 第二条入站路径，与消息那条完全不搭界：不过解析、不过规则、不看 @。
        'card.action.trigger': (event) => ports.onCardAction(event.payload),
        // 第三条。同样不过解析层，而且**不往外抛任何东西** —— 报文里全部字段都可选，
        // 缺字段、定位不到、库报错各自走到终态并留下日志（见 lark/recall-message.ts）。
        // 消息那条路上抛错是为了让泳道交接应答非 2xx，而撤回不走交接（spec 决策 5）：
        // 抛出去没有任何人接得住，只会把"到底是哪种失败"这个区分丢掉。
        'im.message.recalled_v1': (event) =>
            ports.onRecall(event.payload as LarkRecallEvent, event.receivedAt),
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

        // 交接来的信封不过 sinkFor：它是重放，审计在它第一次进来时就记过了；而且这条
        // 路上失败必须原样抛出去变成非 2xx，投递方据此判定投递失败 —— 像飞书那两个入口
        // 那样"先应答再异步处理"就是一条静默丢失。
        //
        // handles 直接取处理表的键，所以"本服务认领了哪些事件"不会跟处理表脱节。
        registerLaneInbound(app) {
            registerLarkLaneInbound(app, {
                lane: ports.lane,
                handles: (eventType) => handlers[eventType] !== undefined,
                deliver,
            });
        },
    };
}
