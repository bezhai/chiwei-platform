import { describe, it, expect, mock, afterAll } from 'bun:test';

// qq 插件 import 期自注册：进 ChannelRegistry / 运行时 registry / CommandRegistry(空指令)。
// 镜像 lark-plugin.test.ts 的最小副作用依赖 mock。

// 注：这里曾有一个 @aliyun/oss 的桩（照抄 lark-plugin.test.ts）。qq 插件的 import
// 图里没有任何一环碰 @aliyun/*（全仓 getOss() 零生产调用方），桩是多余的 —— 而
// mock.module 是进程级全局，多余的桩只会白白污染别的文件，已删。

const redisMock = {
    get: mock(async () => null),
    setWithExpire: mock(async () => undefined),
    hgetall: mock(async () => ({})),
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
};
// Redis 收敛成 @inner/shared/cache 的 RedisClient 单例后，这里桩的是
// getRedisClient()。bun 的 mock.module 是**整模块替换 + 进程级全局**，只写
// getRedisClient 会把同模块的 cache / RedisClient 等导出一并抹掉，
// 别的测试文件跟着遭殃；所以先抓真身、只覆盖这一个导出，afterAll 再注回去。
const realSharedCache = { ...(await import('@inner/shared/cache')) };
const redisStub = redisMock;
mock.module('@inner/shared/cache', () => ({
    ...realSharedCache,
    getRedisClient: () => redisStub,
}));
// laneRouter 必须桩：真身在模块作用域 new LaneRouter(...)，构造函数立刻 fetch
// lite-registry 并起 30s 轮询 timer，单测不该碰网络。同样先抓真身（这一步会构造
// 真 LaneRouter，但全仓 file-pipeline.test.ts 早就这么加载了，不是新增行为），
// afterAll 注回去 —— 它被 jieba / image-pipeline / default-outbound-deps 等多处
// 生产代码 import，留个只有 createClient 的假身会污染后续文件。
const realLaneRouter = { ...(await import('@infrastructure/lane-router')) };
mock.module('@infrastructure/lane-router', () => ({
    ...realLaneRouter,
    laneRouter: { createClient: () => ({ post: mock(async () => ({ data: {} })) }) },
}));

const { qqPlugin } = await import('./index');
const { getChannelRegistry } = await import('@inner/shared/channel');
const { getCommandRegistry } = await import('@inner/shared/rules');
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

afterAll(() => {
    mock.module('@inner/shared/cache', () => realSharedCache);
    mock.module('@infrastructure/lane-router', () => realLaneRouter);
});
