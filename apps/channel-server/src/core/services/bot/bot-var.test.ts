import { describe, it, expect, mock, beforeEach } from 'bun:test';

// 解耦硬约束验证：getBotAppId() / getBotUnionId() 的签名与返回类型一字不改，
// 调用方无感知。内部实现从 credentials JSONB 取（旧独立列已删），对同一 bot
// 的返回值与改造前（取自旧 app_id/robot_union_id 列）完全一致。

let currentBotName = 'chiwei';
const botConfig = {
    bot_name: 'chiwei',
    channel: 'lark',
    credentials: {
        app_id: 'cli_app_v2',
        app_secret: 'sec',
        encrypt_key: 'enc',
        verification_token: 'vt',
        robot_union_id: 'on_union_v2',
    },
};

mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: {
        getBotConfig: (name: string) => (name === botConfig.bot_name ? botConfig : null),
    },
}));
mock.module('@middleware/context', () => ({
    context: { getBotName: () => currentBotName },
}));

let getBotAppId: () => string;
let getBotUnionId: () => string;

// 用绝对文件路径导入被测模块，强制加载真实 bot-var 实现。bun mock.module 是
// 进程级且按 specifier 注册：同进程其他用例（handlers 入站链路）会
// mock.module('@core/services/bot/bot-var', stub) 来切断其依赖链，该 stub 会
// 泄漏到本文件——若用 specifier/相对路径 import，本文件就拿到 stub 而非真实实现。
// 走绝对文件路径触发真实模块求值，覆盖泄漏的 specifier stub，使本测试对兄弟用例
// 执行顺序免疫。
const REAL_BOT_VAR = new URL('./bot-var.ts', import.meta.url).href;

beforeEach(async () => {
    const mod = await import(REAL_BOT_VAR);
    getBotAppId = mod.getBotAppId;
    getBotUnionId = mod.getBotUnionId;
});

describe('bot-var: 签名不变 + 内部改读 credentials JSONB', () => {
    it('getBotAppId() 无参、返回 string，值来自 credentials.app_id', () => {
        const v: string = getBotAppId();
        expect(v).toBe('cli_app_v2');
    });

    it('getBotUnionId() 无参、返回 string，值来自 credentials.robot_union_id', () => {
        const v: string = getBotUnionId();
        expect(v).toBe('on_union_v2');
    });

    it('context 无 botName 时仍按原契约抛错（行为未变）', () => {
        currentBotName = '';
        expect(() => getBotAppId()).toThrow(/Bot name is not set/);
        currentBotName = 'chiwei';
    });
});
