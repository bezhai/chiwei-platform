import * as Lark from '@larksuiteoapi/node-sdk';
import type { Hono } from 'hono';
import { Pool } from 'pg';
import { EventForwarder } from './forwarder';
import { adaptHono } from './lark-adapter';

// channel-proxy 只是飞书 webhook 入口，只认 channel='lark' 的 bot。bot_config
// 多 channel 化后飞书五件套已迁进 credentials JSONB、旧裸列被删，这里按
// credentials 解释凭据，非 lark 的记录直接跳过（不是 webhook 入口的事）。
const LARK_CHANNEL = 'lark';

interface BotConfigRow {
    bot_name: string;
    channel: string;
    credentials: Record<string, unknown> | null;
    init_type: string;
    is_active: boolean;
    is_dev: boolean;
}

interface BotConfig {
    bot_name: string;
    app_id: string;
    app_secret: string;
    encrypt_key: string;
    verification_token: string;
    init_type: string;
    is_active: boolean;
    is_dev: boolean;
}

// 把一条 lark bot_config 记录的 credentials JSONB 解释成飞书四件套。缺字段
// 直接抛错而不是静默放过——凭据缺失静默会让飞书鉴权在运行期出诡异错。
function toLarkBotConfig(row: BotConfigRow): BotConfig {
    const c = row.credentials;
    if (typeof c !== 'object' || c === null) {
        throw new Error(`lark bot "${row.bot_name}" has no credentials JSONB payload`);
    }
    const read = (field: string): string => {
        const v = (c as Record<string, unknown>)[field];
        if (typeof v !== 'string' || v.length === 0) {
            throw new Error(
                `lark bot "${row.bot_name}" missing required credential "${field}"`,
            );
        }
        return v;
    };
    return {
        bot_name: row.bot_name,
        app_id: read('app_id'),
        app_secret: read('app_secret'),
        encrypt_key: read('encrypt_key'),
        verification_token: read('verification_token'),
        init_type: row.init_type,
        is_active: row.is_active,
        is_dev: row.is_dev,
    };
}

type WebSocketClient = {
    start(params: { eventDispatcher: Lark.EventDispatcher }): Promise<void>;
    close(params?: { force?: boolean }): void;
};

type WebSocketClientFactory = (bot: BotConfig) => WebSocketClient;

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

export class BotManager {
    private wsClients: WebSocketClient[] = [];

    constructor(
        private pool: Pool,
        private forwarder: EventForwarder,
        private createWebSocketClient: WebSocketClientFactory = (bot) =>
            new Lark.WSClient({
                appId: bot.app_id,
                appSecret: bot.app_secret,
                loggerLevel: Lark.LoggerLevel.info,
            }),
    ) {}

    async registerBots(app: Hono): Promise<void> {
        const bots = await this.loadBotConfigs();

        const httpBots = bots.filter((b) => b.init_type === 'http');
        const websocketBots = bots.filter((b) => b.init_type === 'websocket');

        if (httpBots.length > 0) {
            for (const bot of httpBots) {
                this.registerHttpBot(app, bot);
            }
            console.info(`Registered ${httpBots.length} HTTP bot(s)`);
        } else {
            console.warn('No HTTP bots found to register');
        }

        if (websocketBots.length > 0) {
            await Promise.all(websocketBots.map((bot) => this.startWebSocketBot(bot)));
            console.info(`Started ${websocketBots.length} WebSocket bot(s)`);
        }
    }

    private async loadBotConfigs(): Promise<BotConfig[]> {
        const isDev = process.env.NODE_ENV !== 'production';
        const result = await this.pool.query<BotConfigRow>(
            "SELECT bot_name, channel, credentials, init_type, is_active, is_dev FROM bot_config WHERE is_active = true AND channel = 'lark'",
        );

        return result.rows
            .filter((row) => row.channel === LARK_CHANNEL)
            .filter((row) => (isDev ? true : !row.is_dev))
            .map((row) => toLarkBotConfig(row));
    }

    closeWebSocketClients(): void {
        for (const client of this.wsClients) {
            client.close({ force: true });
        }
        this.wsClients = [];
    }

    private createEventDispatcher(bot: BotConfig): Lark.EventDispatcher {
        const handler = this.forwarder.createHandler(bot.bot_name);
        const eventHandlers: Record<string, (params: unknown) => Record<string, never>> = {};
        for (const eventType of REGISTERED_EVENT_TYPES) {
            eventHandlers[eventType] = handler;
        }

        return new Lark.EventDispatcher({
            verificationToken: bot.verification_token,
            encryptKey: bot.encrypt_key,
        }).register(eventHandlers);
    }

    private registerHttpBot(app: Hono, bot: BotConfig): void {
        const eventDispatcher = this.createEventDispatcher(bot);

        const cardHandler = this.forwarder.createCardHandler(bot.bot_name);
        const cardActionHandler = new Lark.CardActionHandler(
            {
                verificationToken: bot.verification_token,
                encryptKey: bot.encrypt_key,
            },
            cardHandler,
        );

        const eventPath = `/webhook/${bot.bot_name}/event`;
        const cardPath = `/webhook/${bot.bot_name}/card`;

        app.post(eventPath, adaptHono(eventDispatcher));
        app.post(cardPath, adaptHono(cardActionHandler));

        console.info(
            `Registered bot: ${bot.bot_name} (${bot.app_id}) → ${eventPath}, ${cardPath}`,
        );
    }

    private async startWebSocketBot(bot: BotConfig): Promise<void> {
        try {
            const wsClient = this.createWebSocketClient(bot);
            await wsClient.start({ eventDispatcher: this.createEventDispatcher(bot) });
            this.wsClients.push(wsClient);
            console.info(`Started WebSocket bot: ${bot.bot_name} (${bot.app_id})`);
        } catch (error) {
            console.error(`Failed to start WebSocket bot: ${bot.bot_name} (${bot.app_id})`, error);
        }
    }
}
