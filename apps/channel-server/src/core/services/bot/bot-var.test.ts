import { describe, it, expect, mock, beforeEach, afterAll } from 'bun:test';

// bot-var 是 core 的 common identity helper。平台私有身份（飞书 app_id /
// union_id）必须留在插件层，core 只暴露 bot 在 common_user 里的身份。

let currentBotName = 'chiwei';
const botConfig = {
    bot_name: 'chiwei',
    channel: 'lark',
    common_user_id: '018f-bot-common-user',
};

// bun 的 mock.module 是**整模块替换 + 进程级全局**（mock.restore() 撤不掉），
// 只写 botDirectory 会把同模块的 BotDirectory 等导出一并抹掉，后续加载的文件
// 跟着遭殃；所以先抓真身、只覆盖这一个导出，afterAll 再注回去。
const realSharedBot = { ...(await import('@inner/shared/bot')) };
mock.module('@inner/shared/bot', () => ({
    ...realSharedBot,
    botDirectory: {
        getBotConfig: (name: string) => (name === botConfig.bot_name ? botConfig : null),
    },
}));

// @middleware/context 同理，而且更毒：它的 context 是基座 context 的展开
// （getTraceId / getBotName / getLane / get / getAll / set / run），模块本身还导出
// asyncLocalStorage。整体替换会把这些全抹掉，后续文件报
// "Export named 'asyncLocalStorage' not found"。所以只替换 context.getBotName 这一个
// 取值口径（本用例要控制「当前 bot 是谁」），其余照抄真身，afterAll 注回去。
const realCtx = { ...(await import('@middleware/context')) };
mock.module('@middleware/context', () => ({
    ...realCtx,
    context: { ...realCtx.context, getBotName: () => currentBotName },
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

afterAll(() => {
    mock.module('@inner/shared/bot', () => realSharedBot);
    mock.module('@middleware/context', () => realCtx);
});
