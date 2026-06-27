// QQ bot 身份与凭据解释（QQ 插件私有职责）。
//
// channel-server 侧的 QQ bot 行主要承载 bot_name / channel / persona / common_user_id /
// 网关回调地址；连 QQ 的真实凭据（appId/secret）归 qq-gateway 侧（ConfigBundle/envs）。
// 所以这里对 credentials 宽松解析：可能为空、可能只带 app_id，都不强制必填、不报错
// （与飞书 larkCredentials 的 fail-loud 五字段刻意相反）。

import type { BotConfig } from '@entities/bot-config';
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';

export const QQ_CHANNEL = 'qq';

export interface QqCredentials {
    appId?: string;
    appSecret?: string;
    botSecret?: string;
}

export interface ChannelCredentialed {
    channel: string;
    credentials?: Record<string, unknown> | null;
}

function optionalString(v: unknown): string | undefined {
    return typeof v === 'string' && v.length > 0 ? v : undefined;
}

export function qqCredentials(bot: ChannelCredentialed): QqCredentials {
    if (bot.channel !== QQ_CHANNEL) {
        throw new Error(
            `qqCredentials() called on a non-qq bot (channel="${bot.channel}"); ` +
                `qq credentials only exist on channel="${QQ_CHANNEL}" records`,
        );
    }
    const c = bot.credentials;
    if (typeof c !== 'object' || c === null) {
        // QQ 凭据归网关侧，channel-server 侧允许为空。
        return {};
    }
    return {
        appId: optionalString(c.app_id),
        appSecret: optionalString(c.app_secret),
        botSecret: optionalString(c.bot_secret),
    };
}

// QQ 插件透传 credentials 给框架的解释器：宽松，不强校验。
export function qqParseCredentials(blob: unknown): unknown {
    return blob;
}

function getCurrentQqBotConfig(): BotConfig {
    const botName = context.getBotName();
    if (!botName) {
        throw new Error('Bot name is not set in the context');
    }
    const botConfig = multiBotManager.getBotConfig(botName);
    if (!botConfig) {
        throw new Error(`Bot configuration not found for bot: ${botName}`);
    }
    if (botConfig.channel !== QQ_CHANNEL) {
        throw new Error(`current bot "${botName}" is channel="${botConfig.channel}", not qq`);
    }
    return botConfig;
}

export function getCurrentQqBotName(): string {
    return getCurrentQqBotConfig().bot_name;
}

export function getQqBotConfigByCommonUserId(commonUserId: string): BotConfig | null {
    for (const bot of multiBotManager.getAllBotConfigs()) {
        if (bot.channel !== QQ_CHANNEL) continue;
        if (bot.common_user_id === commonUserId) return bot;
    }
    return null;
}
