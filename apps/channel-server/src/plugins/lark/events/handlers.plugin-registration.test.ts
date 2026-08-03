import { describe, it, expect } from 'bun:test';

// 回归守卫：HTTP 服务的飞书入站入口（handlers.ts）现在经 getChannelRegistry()
// .get(bot.channel) 取插件来 parse/decide。插件靠 import 期自注册；若 HTTP
// 入站链路没有任何地方 import '@plugins/index'，lark 插件就不会注册，handlers 取插件时
// fail-closed 抛错 → 每条入站消息被丢。worker 各自 import 了 @plugins/index，
// 但 HTTP 服务入口（index.ts → application → lark runtime/webhook → handlers）
// 之前并不导入它。本测试钉死：import handlers 这条 Lark 插件入站模块后，lark
// 插件必须已在 ChannelRegistry 注册（即插件事件模块图把自注册副作用拉进来）。

// 本文件一个 mock.module 都不装：只是 import handlers 然后查注册表，import 期
// 不会真去连 redis / oss / DB（那些客户端都是首次调用时才建）。bun 的
// mock.module 是整模块替换 + 进程级全局、mock.restore() 也撤不掉，能不装就不装。

describe('HTTP inbound entry registers the lark channel plugin', () => {
    it('importing handlers makes lark resolvable via ChannelRegistry', async () => {
        // 先 import HTTP 入站模块（handlers），再查注册表。
        await import('./handlers');
        const { getChannelRegistry } = await import('@inner/shared/channel');
        expect(getChannelRegistry().has('lark')).toBe(true);
        // get('lark') 不应 fail-closed 抛错，且拿到的是真实入站实现。
        const plugin = getChannelRegistry().get('lark');
        expect(typeof plugin.inbound.parse).toBe('function');
        expect(typeof plugin.addressing.decide).toBe('function');
    });
});
