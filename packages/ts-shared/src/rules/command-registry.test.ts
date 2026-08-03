import { describe, it, expect } from 'bun:test';
import { CommandRegistry } from './command-registry';
import type { RuleConfig } from './rule';

// 造一条最小 RuleConfig（指令的现有形态）。comment 用作身份断言。
function cmd(comment: string): RuleConfig {
    return {
        rules: [],
        handler: async () => {},
        comment,
    };
}

describe('CommandRegistry', () => {
    it('forChannel 返回该 channel 指令 + 核心通用指令', () => {
        const reg = new CommandRegistry();
        reg.registerCore([cmd('chat')]);
        reg.register('channel-x', [cmd('复读'), cmd('余额')]);

        const got = reg.forChannel('channel-x').map((c) => c.comment);
        expect(got).toEqual(['复读', '余额', 'chat']);
    });

    it('核心通用指令排在最后（catch-all 聊天主链路不抢 utility）', () => {
        const reg = new CommandRegistry();
        reg.register('channel-x', [cmd('撤回')]);
        reg.registerCore([cmd('chat')]);

        const got = reg.forChannel('channel-x').map((c) => c.comment);
        // 平台指令先、核心指令后，与匹配优先级一致
        expect(got[got.length - 1]).toBe('chat');
        expect(got[0]).toBe('撤回');
    });

    it('未注册任何平台指令的 channel 仍拿到核心通用指令', () => {
        const reg = new CommandRegistry();
        reg.registerCore([cmd('chat')]);

        const got = reg.forChannel('channel-y').map((c) => c.comment);
        expect(got).toEqual(['chat']);
    });

    it('不同 channel 的指令互不串台', () => {
        const reg = new CommandRegistry();
        reg.registerCore([cmd('chat')]);
        reg.register('channel-x', [cmd('复读')]);
        reg.register('channel-y', [cmd('y-only')]);

        expect(reg.forChannel('channel-x').map((c) => c.comment)).toEqual(['复读', 'chat']);
        expect(reg.forChannel('channel-y').map((c) => c.comment)).toEqual(['y-only', 'chat']);
    });

    it('同一 channel 多次 register 累加（插件分批注册）', () => {
        const reg = new CommandRegistry();
        reg.register('channel-x', [cmd('复读')]);
        reg.register('channel-x', [cmd('余额')]);
        reg.registerCore([cmd('chat')]);

        expect(reg.forChannel('channel-x').map((c) => c.comment)).toEqual(['复读', '余额', 'chat']);
    });

    it('forChannel 返回副本，外部修改不污染注册表', () => {
        const reg = new CommandRegistry();
        reg.registerCore([cmd('chat')]);
        reg.register('channel-x', [cmd('复读')]);

        const list = reg.forChannel('channel-x');
        list.push(cmd('注入'));

        // 再取一次不应包含被外部 push 的指令
        expect(reg.forChannel('channel-x').map((c) => c.comment)).toEqual(['复读', 'chat']);
    });
});
