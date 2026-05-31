import { describe, it, expect } from 'bun:test';
import { ChannelRegistry } from './channel-registry';
import type { ChannelPlugin } from '@core/ports/channel-plugin';

// 造一个结构上满足 ChannelPlugin 的假插件(只为测注册表,不测真实平台逻辑)。
function fakePlugin(channel: string, withRecall = true): ChannelPlugin {
    return {
        channel,
        inbound: {
            verify: () => true,
            handleHandshake: () => null,
            parse: () => null,
        },
        addressing: {
            decide: () => ({ respond: true, reason: '' }),
        },
        capabilities: {
            sendText: async () => ({ channelId: 'm1' }),
            reply: async () => ({ channelId: 'm1' }),
            ...(withRecall ? { recall: async () => {} } : {}),
        },
        commands: [],
        parseCredentials: (blob: unknown) => blob,
    };
}

describe('ChannelRegistry: 插件自注册 + fail-closed 查找', () => {
    it('register 后 get 拿回同一个插件', () => {
        const reg = new ChannelRegistry();
        const lark = fakePlugin('lark');
        reg.register(lark);
        expect(reg.get('lark')).toBe(lark);
    });

    it('has 反映注册状态', () => {
        const reg = new ChannelRegistry();
        reg.register(fakePlugin('lark'));
        expect(reg.has('lark')).toBe(true);
        expect(reg.has('qq')).toBe(false);
    });

    it('get 未知 channel → fail-closed 抛错,不静默返回 null', () => {
        const reg = new ChannelRegistry();
        expect(() => reg.get('telegram')).toThrow(/unknown channel|未注册|telegram/i);
    });

    it('重复注册同一 channel → 抛错(禁止静默覆盖)', () => {
        const reg = new ChannelRegistry();
        reg.register(fakePlugin('lark'));
        expect(() => reg.register(fakePlugin('lark'))).toThrow(/already|重复|duplicate/i);
    });

    it('channels() 列出所有已注册 channel', () => {
        const reg = new ChannelRegistry();
        reg.register(fakePlugin('lark'));
        reg.register(fakePlugin('qq'));
        expect(reg.channels().sort()).toEqual(['lark', 'qq']);
    });
});

describe('能力可选:平台不支持的能力直接缺失,不是 flag/降级', () => {
    it('支持 recall 的插件能力端口上有 recall', () => {
        const reg = new ChannelRegistry();
        reg.register(fakePlugin('lark', true));
        expect(typeof reg.get('lark').capabilities.recall).toBe('function');
    });

    it('不支持 recall 的插件能力端口上没有 recall', () => {
        const reg = new ChannelRegistry();
        reg.register(fakePlugin('qq', false));
        expect(reg.get('qq').capabilities.recall).toBeUndefined();
    });
});
