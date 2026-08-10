import { describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';

import {
    createLarkBotLookup,
    larkAppIdOf,
    larkDisplayNameOf,
    larkPersonaIdOf,
    type LarkBotRoster,
} from './bot-lookup';

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

// 投影按 (app_id, open_id) 记用户身份。app_id 通常就在事件里，缺了的话只能问
// "接这条事件的是哪个 bot"。
describe('larkAppIdOf', () => {
    it('answers with the Lark app the bot runs as', () => {
        expect(larkAppIdOf(roster([bot()]), 'chiwei')).toBe('cli_chiwei');
    });

    // 猜一个 app_id 会把这个人记到别的应用名下 —— 同一个人从此在公共层有两份身份。
    it('refuses to guess when the bot is unknown', () => {
        expect(() => larkAppIdOf(roster([bot()]), 'nobody')).toThrow(/nobody/);
    });
});

// chat.request 的 persona_ids 走这一跳：被 @ 的人已经是 common_user_id 了，还要
// 知道其中哪些是我们的 bot、各自穿的是哪个人设。
describe('larkPersonaIdOf', () => {
    it('answers with the persona a bot of ours wears', () => {
        expect(larkPersonaIdOf(roster([bot()]), 'cu_chiwei')).toBe('p_chiwei');
    });

    it('says nothing about a common user that is not one of our bots', () => {
        expect(larkPersonaIdOf(roster([bot()]), 'cu_some_human')).toBeUndefined();
    });

    // 工具 bot 没绑人设。它被 @ 到不该让任何人设开口。
    it('says nothing for one of our bots that wears no persona', () => {
        expect(larkPersonaIdOf(roster([bot({ persona_id: undefined })]), 'cu_chiwei')).toBeUndefined();
    });

    it('ignores bots from other channels', () => {
        const bots = [
            { bot_name: 'qq-bot', channel: 'qq', common_user_id: 'cu_qq', persona_id: 'p_qq' } as BotConfig,
            bot(),
        ];
        expect(larkPersonaIdOf(roster(bots), 'cu_qq')).toBeUndefined();
    });
});

// 出站消息上署的名。飞书后台那个应用名（"赤尾机器人"之类）不是给人看的。
describe('larkDisplayNameOf', () => {
    const personas = (personaId: string) => (personaId === 'p_chiwei' ? '赤尾' : null);

    it('answers with the persona display name the bot wears', () => {
        expect(larkDisplayNameOf(roster([bot()]), personas, 'chiwei')).toBe('赤尾');
    });

    it('says nothing for a bot that wears no persona', () => {
        expect(
            larkDisplayNameOf(roster([bot({ persona_id: undefined })]), personas, 'chiwei'),
        ).toBeUndefined();
    });

    it('says nothing when the persona has no display name on record', () => {
        expect(
            larkDisplayNameOf(roster([bot({ persona_id: 'p_unknown' })]), personas, 'chiwei'),
        ).toBeUndefined();
    });

    // 署名缺失只是消息上少一个名字；抛错会让整条回复发不出去。
    it('says nothing about a bot this process never loaded', () => {
        expect(larkDisplayNameOf(roster([bot()]), personas, 'nobody')).toBeUndefined();
    });

    it('ignores bots from other channels', () => {
        const bots = [
            { bot_name: 'chiwei', channel: 'qq', persona_id: 'p_chiwei' } as BotConfig,
        ];
        expect(larkDisplayNameOf(roster(bots), personas, 'chiwei')).toBeUndefined();
    });
});
