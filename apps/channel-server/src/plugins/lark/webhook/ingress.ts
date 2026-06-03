// 飞书 webhook / ws 入站收口。channel-server 自己接飞书事件：按 bot 起飞书
// SDK 的 EventDispatcher（HTTP 模式注册 /webhook/{bot}/
// event|card 路由）和 WSClient（长连模式），SDK 解析+验签+解密后回调，回调把事件
// 投给 dispatchLarkEvent → 本进程入站链路。
//
// 数据源用已加载的 BotConfig（凭据经 larkCredentials 从 credentials JSONB 取），
// 不自己起 pg.Pool 查 bot_config（避免与 multi-bot-manager 重复一套 bot 加载）。
//
// HTTP webhook 路由是被动入口，是否有流量由 api-gateway 规则决定。WSClient 是
// 主动入口，仍由 LARK_DIRECT_INGRESS 控制。

import * as Lark from '@larksuiteoapi/node-sdk';
import type { Hono } from 'hono';
import { BotConfig } from '@entities/bot-config';
import { larkCredentials, type LarkCredentials } from '../bot-identity';
import { adaptHono } from './lark-adapter';
import { dispatchLarkEvent } from './dispatch';

// 飞书事件清单：同一个 handler 注册到所有类型，回调内按
// params.event_type 区分。
const REGISTERED_EVENT_TYPES = [
    'im.message.receive_v1',
    'im.message.recalled_v1',
    'im.chat.member.user.added_v1',
    'im.chat.member.user.deleted_v1',
    'im.chat.member.user.withdrawn_v1',
    'im.chat.member.bot.added_v1',
    'im.chat.member.bot.deleted_v1',
    'im.message.reaction.created_v1',
    'im.message.reaction.deleted_v1',
    'im.chat.access_event.bot_p2p_chat_entered_v1',
    'im.chat.updated_v1',
    'card.action.trigger',
];

type SdkAck = Record<string, never>;

// SDK 事件回调 → dispatch。同一 handler 用于所有事件类型，按 params.event_type
// 区分。fire-and-forget：SDK 要求快速 ack，
// 返回 {} 立刻应答，真正处理异步走 dispatch。
export function createLarkEventHandler(botName: string): (params: unknown) => SdkAck {
    return (params: unknown): SdkAck => {
        const eventType = (params as { event_type?: string })?.event_type || 'unknown';
        dispatchLarkEvent({ eventType, params, botName }).catch((err) => {
            console.error(`[lark-ingress] dispatch ${eventType} for ${botName} failed:`, err);
        });
        return {};
    };
}

// 卡片动作回调 → 固定 card.action.trigger。
export function createLarkCardHandler(botName: string): (data: unknown) => SdkAck {
    return (data: unknown): SdkAck => {
        dispatchLarkEvent({ eventType: 'card.action.trigger', params: data, botName }).catch(
            (err) => {
                console.error(`[lark-ingress] dispatch card action for ${botName} failed:`, err);
            },
        );
        return {};
    };
}

type WebSocketClient = {
    start(params: { eventDispatcher: Lark.EventDispatcher }): Promise<void>;
    close(params?: { force?: boolean }): void;
};

type WebSocketClientFactory = (creds: LarkCredentials) => WebSocketClient;

export class LarkEventIngress {
    private wsClients: WebSocketClient[] = [];

    constructor(
        private createWebSocketClient: WebSocketClientFactory = (creds) =>
            new Lark.WSClient({
                appId: creds.app_id,
                appSecret: creds.app_secret,
                loggerLevel: Lark.LoggerLevel.info,
            }),
    ) {}

    // 注册 HTTP 模式 bot 的 webhook 路由到 app。
    registerHttpBots(app: Hono, bots: BotConfig[]): void {
        for (const bot of bots) {
            this.registerHttpBot(app, bot);
        }
        console.info(`[lark-ingress] registered ${bots.length} HTTP bot webhook(s)`);
    }

    // 起 ws 模式 bot 的长连接。
    async startWebSocketBots(bots: BotConfig[]): Promise<void> {
        await Promise.all(bots.map((bot) => this.startWebSocketBot(bot)));
        if (bots.length > 0) {
            console.info(`[lark-ingress] started ${bots.length} WebSocket bot(s)`);
        }
    }

    closeWebSocketClients(): void {
        for (const client of this.wsClients) {
            client.close({ force: true });
        }
        this.wsClients = [];
    }

    private createEventDispatcher(bot: BotConfig, creds: LarkCredentials): Lark.EventDispatcher {
        const handler = createLarkEventHandler(bot.bot_name);
        const eventHandlers: Record<string, (params: unknown) => SdkAck> = {};
        for (const eventType of REGISTERED_EVENT_TYPES) {
            eventHandlers[eventType] = handler;
        }
        return new Lark.EventDispatcher({
            verificationToken: creds.verification_token,
            encryptKey: creds.encrypt_key,
        }).register(eventHandlers);
    }

    private registerHttpBot(app: Hono, bot: BotConfig): void {
        const creds = larkCredentials(bot);
        const eventDispatcher = this.createEventDispatcher(bot, creds);

        const cardActionHandler = new Lark.CardActionHandler(
            {
                verificationToken: creds.verification_token,
                encryptKey: creds.encrypt_key,
            },
            createLarkCardHandler(bot.bot_name),
        );

        const eventPath = `/webhook/${bot.bot_name}/event`;
        const cardPath = `/webhook/${bot.bot_name}/card`;
        app.post(eventPath, adaptHono(eventDispatcher));
        app.post(cardPath, adaptHono(cardActionHandler));

        console.info(
            `[lark-ingress] ${bot.bot_name} (${creds.app_id}) → ${eventPath}, ${cardPath}`,
        );
    }

    private async startWebSocketBot(bot: BotConfig): Promise<void> {
        const creds = larkCredentials(bot);
        try {
            const wsClient = this.createWebSocketClient(creds);
            await wsClient.start({ eventDispatcher: this.createEventDispatcher(bot, creds) });
            this.wsClients.push(wsClient);
            console.info(`[lark-ingress] started WebSocket bot: ${bot.bot_name} (${creds.app_id})`);
        } catch (error) {
            console.error(
                `[lark-ingress] failed to start WebSocket bot: ${bot.bot_name} (${creds.app_id})`,
                error,
            );
        }
    }
}
