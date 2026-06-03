import { describe, it, expect, mock, beforeEach, afterEach } from 'bun:test';
import type { BotConfig } from '@entities/bot-config';
import type { ChannelCredentialed, LarkCredentials } from './bot-identity';

let currentBotName = 'chiwei';
let allBots: Partial<BotConfig>[] = [];
const personas = [
    { persona_id: 'persona-1', display_name: '赤尾' },
    { persona_id: 'persona-2', display_name: '工具' },
];

mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: {
        getBotConfig: (name: string) => allBots.find((bot) => bot.bot_name === name) ?? null,
        getAllBotConfigs: () => allBots,
    },
}));

mock.module('@middleware/context', () => ({
    context: {
        getBotName: () => currentBotName,
    },
}));

mock.module('ormconfig', () => ({
    default: {
        getRepository: () => ({
            findBy: async () => personas,
        }),
    },
}));

const {
    getCurrentLarkBotAppId,
    getCurrentLarkBotUnionId,
    getLarkBotConfigByAppId,
    getLarkBotConfigByCommonUserId,
    getLarkBotConfigByUnionId,
    getLarkDisplayNameByAppId,
    larkCredentials,
    loadLarkDisplayNames,
    resetLarkDisplayNames,
} = await import('./bot-identity');

function larkBot(over: Partial<BotConfig> = {}): Partial<BotConfig> {
    return {
        bot_name: 'chiwei',
        channel: 'lark',
        persona_id: 'persona-1',
        common_user_id: '018f-bot-common-user',
        credentials: {
            app_id: 'cli_app_123',
            app_secret: 'sec_456',
            encrypt_key: 'enc_789',
            verification_token: 'vtok_abc',
            robot_union_id: 'on_union_def',
        },
        ...over,
    };
}

beforeEach(() => {
    currentBotName = 'chiwei';
    allBots = [larkBot()];
});

afterEach(() => {
    resetLarkDisplayNames();
});

describe('lark bot identity: plugin-owned credentials and lookup', () => {
    it('typed view 完整取出五个飞书凭据字段', () => {
        const c: LarkCredentials = larkCredentials(larkBot() as ChannelCredentialed);
        expect(c.app_id).toBe('cli_app_123');
        expect(c.app_secret).toBe('sec_456');
        expect(c.encrypt_key).toBe('enc_789');
        expect(c.verification_token).toBe('vtok_abc');
        expect(c.robot_union_id).toBe('on_union_def');
    });

    it('非 lark channel 的记录取飞书凭据时明确报错', () => {
        const qq: ChannelCredentialed = {
            channel: 'qq',
            credentials: { app_id: 'qq_1', app_secret: 'qq_2', bot_secret: 'qq_3' },
        };
        expect(() => larkCredentials(qq)).toThrow(/lark|飞书/i);
    });

    it('lark 记录但 credentials 缺关键字段时明确报错', () => {
        const broken: ChannelCredentialed = {
            channel: 'lark',
            credentials: { app_id: 'only_app_id' },
        };
        expect(() => larkCredentials(broken)).toThrow();
    });

    it('current bot app_id / union_id 只在 Lark 插件层读取', () => {
        expect(getCurrentLarkBotAppId()).toBe('cli_app_123');
        expect(getCurrentLarkBotUnionId()).toBe('on_union_def');
    });

    it('can reverse lookup registered lark bots by app_id and union_id', () => {
        allBots = [
            larkBot({ bot_name: 'chiwei' }),
            larkBot({
                bot_name: 'other',
                common_user_id: '018f-other-common',
                credentials: {
                    app_id: 'cli_other',
                    app_secret: 'sec',
                    encrypt_key: 'enc',
                    verification_token: 'vt',
                    robot_union_id: 'on_other',
                },
            }),
            { bot_name: 'qq', channel: 'qq' },
        ];

        expect(getLarkBotConfigByAppId('cli_other')?.bot_name).toBe('other');
        expect(getLarkBotConfigByUnionId('on_other')?.bot_name).toBe('other');
        expect(getLarkBotConfigByCommonUserId('018f-other-common')?.bot_name).toBe('other');
        expect(getLarkBotConfigByAppId('missing')).toBeNull();
    });

    it('loads app_id -> persona display_name in plugin runtime cache', async () => {
        allBots = [
            larkBot({ persona_id: 'persona-1' }),
            larkBot({
                bot_name: 'utility',
                persona_id: 'persona-2',
                credentials: {
                    app_id: 'cli_utility',
                    app_secret: 'sec',
                    encrypt_key: 'enc',
                    verification_token: 'vt',
                    robot_union_id: 'on_utility',
                },
            }),
        ];

        await loadLarkDisplayNames();

        expect(getLarkDisplayNameByAppId('cli_app_123')).toBe('赤尾');
        expect(getLarkDisplayNameByAppId('cli_utility')).toBe('工具');
    });
});
