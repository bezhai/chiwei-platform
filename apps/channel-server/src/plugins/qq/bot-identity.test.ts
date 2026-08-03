import { describe, it, expect, mock, beforeEach, afterAll } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';

let allBots: Partial<BotConfig>[] = [];
let currentBotName = 'chiwei-qq';

// bun 的 mock.module 是**整模块替换 + 进程级全局**（mock.restore() 撤不掉），
// 只写 botDirectory 会把同模块的 BotDirectory 等导出一并抹掉，后续加载的文件
// 跟着遭殃；所以先抓真身、只覆盖这一个导出，afterAll 再注回去。
const realSharedBot = { ...(await import('@inner/shared/bot')) };
mock.module('@inner/shared/bot', () => ({
    ...realSharedBot,
    botDirectory: {
        getBotConfig: (name: string) => allBots.find((b) => b.bot_name === name) ?? null,
        getAllBotConfigs: () => allBots,
    },
}));

// @middleware/context 同理，而且更毒：它的 context 是基座 context 的展开，模块还
// 导出 asyncLocalStorage；整体替换会让后续文件报 "Export named 'asyncLocalStorage'
// not found"。只替换 context.getBotName 这一个取值口径，其余照抄真身、afterAll 注回。
const realCtx = { ...(await import('@middleware/context')) };
mock.module('@middleware/context', () => ({
    ...realCtx,
    context: { ...realCtx.context, getBotName: () => currentBotName },
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

afterAll(() => {
    mock.module('@inner/shared/bot', () => realSharedBot);
    mock.module('@middleware/context', () => realCtx);
});
