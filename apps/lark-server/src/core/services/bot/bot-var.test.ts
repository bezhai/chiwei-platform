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

beforeEach(async () => {
    const mod = await import('./bot-var');
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
