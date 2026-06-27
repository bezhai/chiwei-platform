import { describe, it, expect, mock, beforeEach } from 'bun:test';
import type { BotConfig } from '@entities/bot-config';

let allBots: Partial<BotConfig>[] = [];
let currentBotName = 'chiwei-qq';

mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: {
        getBotConfig: (name: string) => allBots.find((b) => b.bot_name === name) ?? null,
        getAllBotConfigs: () => allBots,
    },
}));
mock.module('@middleware/context', () => ({
    context: { getBotName: () => currentBotName },
}));

const {
    qqCredentials,
    getCurrentQqBotName,
    getQqBotConfigByCommonUserId,
} = await import('./bot-identity');

function qqBot(over: Partial<BotConfig> = {}): Partial<BotConfig> {
    return {
        bot_name: 'chiwei-qq',
        channel: 'qq',
        persona_id: 'persona-1',
        common_user_id: '018f-qq-bot',
        credentials: { app_id: 'qq_app_1' },
        ...over,
    };
}

beforeEach(() => {
    allBots = [qqBot()];
    currentBotName = 'chiwei-qq';
});

describe('qq bot identity: lenient credentials', () => {
    it('parses app_id when present', () => {
        expect(qqCredentials(qqBot() as never).appId).toBe('qq_app_1');
    });

    it('tolerates empty / missing credentials (returns empty fields, no throw)', () => {
        expect(qqCredentials({ channel: 'qq', credentials: null } as never)).toEqual({});
        expect(qqCredentials({ channel: 'qq' } as never)).toEqual({});
    });

    it('throws when called on a non-qq bot record', () => {
        expect(() => qqCredentials({ channel: 'lark', credentials: {} } as never)).toThrow(/qq/i);
    });

    it('reads the current qq bot name from context', () => {
        expect(getCurrentQqBotName()).toBe('chiwei-qq');
    });

    it('reverse-looks-up a qq bot by common_user_id', () => {
        allBots = [qqBot(), { bot_name: 'lark-bot', channel: 'lark', common_user_id: '018f-qq-bot' }];
        expect(getQqBotConfigByCommonUserId('018f-qq-bot')?.bot_name).toBe('chiwei-qq');
        expect(getQqBotConfigByCommonUserId('missing')).toBeNull();
    });
});
