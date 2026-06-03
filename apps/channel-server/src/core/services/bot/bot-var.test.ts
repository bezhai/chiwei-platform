import { describe, it, expect, mock, beforeEach } from 'bun:test';

// bot-var 是 core 的 common identity helper。平台私有身份（飞书 app_id /
// union_id）必须留在插件层，core 只暴露 bot 在 common_user 里的身份。

let currentBotName = 'chiwei';
const botConfig = {
    bot_name: 'chiwei',
    channel: 'lark',
    common_user_id: '018f-bot-common-user',
};

mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: {
        getBotConfig: (name: string) => (name === botConfig.bot_name ? botConfig : null),
    },
}));
mock.module('@middleware/context', () => ({
    context: {
        getBotName: () => currentBotName,
        getLane: () => undefined,
        createContext: (botName?: string, traceId?: string, lane?: string) => ({
            botName,
            traceId: traceId ?? 't',
            lane,
        }),
        run: async (_ctx: unknown, cb: () => Promise<unknown>) => cb(),
    },
}));

let getBotCommonUserId: () => string;

const REAL_BOT_VAR = new URL('./bot-var.ts', import.meta.url).href;

beforeEach(async () => {
    const mod = await import(REAL_BOT_VAR);
    getBotCommonUserId = mod.getBotCommonUserId;
});

describe('bot-var: core only exposes common bot identity', () => {
    it('getBotCommonUserId() 无参、返回 string，值来自 bot_config.common_user_id', () => {
        const v: string = getBotCommonUserId();
        expect(v).toBe('018f-bot-common-user');
    });

    it('context 无 botName 时仍按原契约抛错（行为未变）', () => {
        currentBotName = '';
        expect(() => getBotCommonUserId()).toThrow(/Bot name is not set/);
        currentBotName = 'chiwei';
    });
});
