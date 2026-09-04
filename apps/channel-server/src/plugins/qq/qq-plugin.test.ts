import { describe, it, expect, mock, afterAll } from 'bun:test';

// qq 插件 import 期自注册：进 ChannelRegistry / 运行时 registry / CommandRegistry(空指令)。
// 下面只桩 import 期真会产生副作用的那几个模块，其余一律走真身。

// 注：这里曾有一个 @aliyun/oss 的桩。qq 插件的 import 图里没有任何一环碰
// @aliyun/*（全仓 getOss() 零生产调用方），桩是多余的 —— 而 mock.module 是进程级
// 全局，多余的桩只会白白污染别的文件，已删。

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
// lite-registry 并起 30s 轮询 timer，单测不该碰网络。先抓真身、afterAll 再注回去
// —— 它被 image-pipeline / default-outbound-deps 等生产代码 import，留个只有
// createClient 的假身会污染后续文件。
const realLaneRouter = { ...(await import('@infrastructure/lane-router')) };
mock.module('@infrastructure/lane-router', () => ({
    ...realLaneRouter,
    laneRouter: { createClient: () => ({ post: mock(async () => ({ data: {} })) }) },
}));

const { Hono } = await import('hono');
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

    it('import 即把 qq runtime 注册进 runtime registry，带 http ingress', () => {
        const runtime = getChannelRuntime('qq');
        expect(runtime.channel).toBe('qq');
        expect(typeof runtime.registerHttpIngress).toBe('function');
    });

    // 两条端点，两份契约：/inbound 收 qq-gateway 投来的 CustomInboundMessage，
    // /lane-inbound 收 prod 判过泳道之后交接来的信封。两条都在内网 Bearer 之后。
    it('registerHttpIngress 同时挂上原始入站端点与泳道信封端点，都要鉴权', async () => {
        const runtime = getChannelRuntime('qq');
        const app = new Hono();

        await runtime.registerHttpIngress!(app, []);

        for (const path of ['/api/internal/qq/inbound', '/api/internal/qq/lane-inbound']) {
            const res = await app.request(path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            expect(res.status).toBe(401);
        }
    });

    // 拆掉之前 core 里还挂着一条聊天主链路 catch-all（NeedRobotMention + publish
    // chat_request），所以这里的序列长度至少是 1。那条支线整条拆了 —— 赤尾不从队列拿
    // 消息，她每一缝直接查 common_message、自己决定要不要开口。QQ 自己一条平台指令都
    // 没注册，于是这条序列现在是**空的**：runRules 对每条 QQ 消息只留一条终态日志。
    it('qq 没有平台指令，也没有任何核心兜底：forChannel 是空的', () => {
        expect(qqPlugin.commands).toEqual([]);
        expect(getCommandRegistry().forChannel('qq')).toEqual([]);
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
