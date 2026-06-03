import { In } from 'typeorm';
import AppDataSource from 'ormconfig';
import type { BotConfig } from '@entities/bot-config';
import { BotPersona } from '@entities/bot-persona';
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';

export const LARK_CHANNEL = 'lark';

export interface LarkCredentials {
    app_id: string;
    app_secret: string;
    encrypt_key: string;
    verification_token: string;
    robot_union_id: string;
}

export interface ChannelCredentialed {
    channel: string;
    credentials?: Record<string, unknown> | null;
}

const REQUIRED_FIELDS: (keyof LarkCredentials)[] = [
    'app_id',
    'app_secret',
    'encrypt_key',
    'verification_token',
    'robot_union_id',
];

const appIdToDisplayName = new Map<string, string>();

export function larkCredentials(bot: ChannelCredentialed): LarkCredentials {
    if (bot.channel !== LARK_CHANNEL) {
        throw new Error(
            `larkCredentials() called on a non-lark bot (channel="${bot.channel}"); ` +
                `lark credentials only exist on channel="${LARK_CHANNEL}" records`,
        );
    }
    const c = bot.credentials;
    if (typeof c !== 'object' || c === null) {
        throw new Error('lark bot has no credentials JSONB payload');
    }
    const out = {} as LarkCredentials;
    for (const f of REQUIRED_FIELDS) {
        const v = (c as Record<string, unknown>)[f];
        if (typeof v !== 'string' || v.length === 0) {
            throw new Error(`lark credentials missing required field "${f}" (channel=lark)`);
        }
        out[f] = v;
    }
    return out;
}

function getCurrentLarkBotConfig(): BotConfig {
    const botName = context.getBotName();
    if (!botName) {
        throw new Error('Bot name is not set in the context');
    }
    const botConfig = multiBotManager.getBotConfig(botName);
    if (!botConfig) {
        throw new Error(`Bot configuration not found for bot: ${botName}`);
    }
    if (botConfig.channel !== LARK_CHANNEL) {
        throw new Error(`current bot "${botName}" is channel="${botConfig.channel}", not lark`);
    }
    return botConfig;
}

export function getCurrentLarkBotAppId(): string {
    return larkCredentials(getCurrentLarkBotConfig()).app_id;
}

export function getCurrentLarkBotUnionId(): string {
    return larkCredentials(getCurrentLarkBotConfig()).robot_union_id;
}

export function getLarkBotConfigByAppId(appId: string): BotConfig | null {
    for (const bot of multiBotManager.getAllBotConfigs()) {
        if (bot.channel !== LARK_CHANNEL) continue;
        if (larkCredentials(bot).app_id === appId) return bot;
    }
    return null;
}

export function getLarkBotConfigByUnionId(unionId: string): BotConfig | null {
    for (const bot of multiBotManager.getAllBotConfigs()) {
        if (bot.channel !== LARK_CHANNEL) continue;
        if (larkCredentials(bot).robot_union_id === unionId) return bot;
    }
    return null;
}

export function getLarkBotConfigByCommonUserId(commonUserId: string): BotConfig | null {
    for (const bot of multiBotManager.getAllBotConfigs()) {
        if (bot.channel !== LARK_CHANNEL) continue;
        if (bot.common_user_id === commonUserId) return bot;
    }
    return null;
}

export async function loadLarkDisplayNames(): Promise<void> {
    appIdToDisplayName.clear();
    const bots = multiBotManager
        .getAllBotConfigs()
        .filter((bot) => bot.channel === LARK_CHANNEL && bot.persona_id);
    const personaIds = bots.map((bot) => bot.persona_id!);
    if (personaIds.length === 0) return;

    const personaRepo = AppDataSource.getRepository(BotPersona);
    const personas = await personaRepo.findBy({
        persona_id: In(personaIds),
    });
    const personaMap = new Map(personas.map((p) => [p.persona_id, p.display_name]));

    for (const bot of bots) {
        const displayName = personaMap.get(bot.persona_id!);
        if (!displayName) continue;
        appIdToDisplayName.set(larkCredentials(bot).app_id, displayName);
    }
}

export function getLarkDisplayNameByAppId(appId: string): string | null {
    return appIdToDisplayName.get(appId) ?? null;
}

export function resetLarkDisplayNames(): void {
    appIdToDisplayName.clear();
}
