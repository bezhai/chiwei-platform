import * as Lark from '@larksuiteoapi/node-sdk';
import Router from '@koa/router';
import { Pool } from 'pg';
import { EventForwarder } from './forwarder';

/**
 * 从 DB 加载的 bot 配置（仅需 proxy 用到的字段）
 */
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

/**
 * proxy 需要注册的固定事件列表（从 handlers.ts 提取）
 */
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
];

/**
 * Bot 管理器
 * 从 DB 加载 bot_config，为每个 HTTP bot 创建 EventDispatcher + Koa 路由
 */
export class BotManager {
    constructor(
        private pool: Pool,
        private forwarder: EventForwarder,
    ) {}

    /**
     * 从 DB 加载 bot 配置并注册到 router
     */
    async registerBots(router: Router): Promise<void> {
        const bots = await this.loadBotConfigs();

        const httpBots = bots.filter((b) => b.init_type === 'http');
        if (httpBots.length === 0) {
            console.warn('No HTTP bots found to register');
            return;
        }

        for (const bot of httpBots) {
            this.registerBot(router, bot);
        }

        console.info(`Registered ${httpBots.length} HTTP bot(s)`);
    }

    private async loadBotConfigs(): Promise<BotConfig[]> {
        const isDev = process.env.NODE_ENV !== 'production';
        const result = await this.pool.query<BotConfig>(
            'SELECT bot_name, app_id, app_secret, encrypt_key, verification_token, init_type, is_active, is_dev FROM bot_config WHERE is_active = true',
        );

        return result.rows.filter((bot) => {
            if (isDev) return true;
            return !bot.is_dev;
        });
    }

    private registerBot(router: Router, bot: BotConfig): void {
        // 创建 EventDispatcher 并注册所有事件
        const handler = this.forwarder.createHandler(bot.bot_name);
        const eventHandlers: Record<string, (params: unknown) => Record<string, never>> = {};
        for (const eventType of REGISTERED_EVENT_TYPES) {
            eventHandlers[eventType] = handler;
        }

        const eventDispatcher = new Lark.EventDispatcher({
            verificationToken: bot.verification_token,
            encryptKey: bot.encrypt_key,
        }).register(eventHandlers);

        // 创建 CardActionHandler
        const cardHandler = this.forwarder.createCardHandler(bot.bot_name);
        const cardActionHandler = new Lark.CardActionHandler(
            {
                verificationToken: bot.verification_token,
                encryptKey: bot.encrypt_key,
            },
            cardHandler,
        );

        // 转为 Koa 中间件
        const eventMiddleware = Lark.adaptKoaRouter(eventDispatcher, { autoChallenge: true });
        const cardMiddleware = Lark.adaptKoaRouter(cardActionHandler, { autoChallenge: true });

        // 注册路由
        const eventPath = `/webhook/${bot.bot_name}/event`;
        const cardPath = `/webhook/${bot.bot_name}/card`;

        router.post(eventPath, eventMiddleware);
        router.post(cardPath, cardMiddleware);

        console.info(
            `Registered bot: ${bot.bot_name} (${bot.app_id}) → ${eventPath}, ${cardPath}`,
        );
    }
}
