import { describe, it, expect } from 'bun:test';
import { larkPlugin } from './index';
import { getChannelRegistry } from '@core/registry/channel-registry';
import { getCommandRegistry } from '@core/registry/command-registry';

// B1 行为契约：飞书插件 import 期自注册——把自己注册进 ChannelRegistry 单例，
// 并把它的 10 条平台指令注册进 CommandRegistry 单例(channel='lark')。
// 这是「加平台 = 新增 plugins/xxx + 在 plugins/index.ts import 一行」的命门。

describe('lark 插件自注册', () => {
    it('import 即把 lark 插件注册进 ChannelRegistry 单例', () => {
        const reg = getChannelRegistry();
        expect(reg.has('lark')).toBe(true);
        expect(reg.get('lark')).toBe(larkPlugin);
        expect(larkPlugin.channel).toBe('lark');
    });

    it('插件自带 10 条平台指令，且经 CommandRegistry 注册到 lark', () => {
        expect(larkPlugin.commands.length).toBe(10);

        // forChannel('lark') = lark 平台指令在前 + 核心通用指令在后。
        // 前 10 条必须就是插件自己声明的 10 条(顺序一致)。
        const out = getCommandRegistry().forChannel('lark');
        const larkComments = larkPlugin.commands.map((c) => c.comment);
        expect(out.slice(0, 10).map((c) => c.comment)).toEqual(larkComments);
    });

    it('平台指令不再带 channels flag（归属靠注册，不靠 flag）', () => {
        for (const c of larkPlugin.commands) {
            expect(c.channels).toBeUndefined();
        }
    });
});
