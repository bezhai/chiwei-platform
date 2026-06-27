import { describe, it, expect, mock } from 'bun:test';

// qq 插件 import 期自注册：进 ChannelRegistry / 运行时 registry / CommandRegistry(空指令)。
// 镜像 lark-plugin.test.ts 的最小副作用依赖 mock。

mock.module('@aliyun/oss', () => ({
    getOss: () => ({ getFile: mock(async () => undefined) }),
}));
const redisMock = {
    get: mock(async () => null),
    setWithExpire: mock(async () => undefined),
    hgetall: mock(async () => ({})),
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
};
mock.module('@cache/redis-client', () => redisMock);
mock.module('infrastructure/cache/redis-client', () => redisMock);
mock.module('@infrastructure/lane-router', () => ({
    laneRouter: { createClient: () => ({ post: mock(async () => ({ data: {} })) }) },
}));

const { qqPlugin } = await import('./index');
const { getChannelRegistry } = await import('@core/registry/channel-registry');
const { getCommandRegistry } = await import('@core/registry/command-registry');
const { getChannelRuntime } = await import('@plugins/runtime');

describe('qq 插件自注册', () => {
    it('import 即把 qq 插件注册进 ChannelRegistry 单例', () => {
        const reg = getChannelRegistry();
        expect(reg.has('qq')).toBe(true);
        expect(reg.get('qq')).toBe(qqPlugin);
        expect(qqPlugin.channel).toBe('qq');
    });

    it('import 即把 qq runtime 注册进 runtime registry，带 http ingress + lane envelope 处理', () => {
        const runtime = getChannelRuntime('qq');
        expect(runtime.channel).toBe('qq');
        expect(typeof runtime.registerHttpIngress).toBe('function');
        expect(typeof runtime.handleInboundLaneEnvelope).toBe('function');
    });

    it('qq 没有平台指令（commands=[]），forChannel 只剩核心通用聊天主链路', () => {
        expect(qqPlugin.commands).toEqual([]);
        const out = getCommandRegistry().forChannel('qq');
        expect(out.length).toBeGreaterThanOrEqual(1);
        expect(out[out.length - 1].category).toBe('persona');
    });

    it('qq 出站能力四件齐备', () => {
        expect(typeof qqPlugin.capabilities.resolveOutboundTarget).toBe('function');
        expect(typeof qqPlugin.capabilities.sendText).toBe('function');
        expect(typeof qqPlugin.capabilities.reply).toBe('function');
        expect(typeof qqPlugin.capabilities.recordOutboundMessage).toBe('function');
    });
});
