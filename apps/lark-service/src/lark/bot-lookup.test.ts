import { describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';

import { createLarkBotLookup, type LarkBotRoster } from './bot-lookup';

function bot(overrides: Partial<BotConfig> = {}): BotConfig {
    return {
        bot_name: 'chiwei',
        channel: 'lark',
        common_user_id: 'cu_chiwei',
        persona_id: 'p_chiwei',
        credentials: {
            app_id: 'cli_chiwei',
            app_secret: 's',
            encrypt_key: 'e',
            verification_token: 'v',
            robot_union_id: 'on_chiwei',
        },
        ...overrides,
    } as BotConfig;
}

function roster(bots: BotConfig[]): LarkBotRoster {
    return { getAllBotConfigs: () => bots };
}

const noPersonas = () => null;

describe('createLarkBotLookup', () => {
    it('finds one of our bots by its Lark app id', () => {
        const lookup = createLarkBotLookup(roster([bot()]), noPersonas);
        expect(lookup.byAppId('cli_chiwei')).toEqual({
            botName: 'chiwei',
            displayName: null,
            commonUserId: 'cu_chiwei',
        });
    });

    it('finds one of our bots by its Lark union id', () => {
        const lookup = createLarkBotLookup(roster([bot()]), noPersonas);
        expect(lookup.byUnionId('on_chiwei')!.botName).toBe('chiwei');
    });

    it('says so when the bot is not one of ours', () => {
        const lookup = createLarkBotLookup(roster([bot()]), noPersonas);
        expect(lookup.byAppId('cli_someone_else')).toBeNull();
        expect(lookup.byUnionId('on_someone_else')).toBeNull();
    });

    it('answers with the persona name the bot wears', () => {
        const lookup = createLarkBotLookup(roster([bot()]), (personaId) =>
            personaId === 'p_chiwei' ? '赤尾' : null,
        );
        expect(lookup.byAppId('cli_chiwei')!.displayName).toBe('赤尾');
    });

    it('leaves the display name empty for a bot with no persona', () => {
        const lookup = createLarkBotLookup(
            roster([bot({ persona_id: undefined })]),
            () => '赤尾',
        );
        expect(lookup.byAppId('cli_chiwei')!.displayName).toBeNull();
    });

    // 本进程本来就只加载飞书 bot；万一 bot 目录里混进了别的渠道，问它要飞书凭据会
    // 抛错。跳过而不是抛 —— 一条来路不明的记录不该让每条入站消息都炸。
    it('ignores bots from other channels instead of asking them for Lark credentials', () => {
        const lookup = createLarkBotLookup(
            roster([{ bot_name: 'qq-bot', channel: 'qq', credentials: {} } as BotConfig, bot()]),
            noPersonas,
        );
        expect(lookup.byAppId('cli_chiwei')!.botName).toBe('chiwei');
    });

    // bot 目录是启动时一次性加载的，但 common_user_id 可能在加载过程中才回填。
    // 每次查都重读当前配置，不缓存快照。
    it('reads the roster on every lookup', () => {
        const bots: BotConfig[] = [];
        const lookup = createLarkBotLookup({ getAllBotConfigs: () => bots }, noPersonas);

        expect(lookup.byAppId('cli_chiwei')).toBeNull();
        bots.push(bot());
        expect(lookup.byAppId('cli_chiwei')).not.toBeNull();
    });
});
