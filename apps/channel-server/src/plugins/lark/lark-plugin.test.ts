import { describe, it, expect, mock, afterAll } from 'bun:test';

// B1 行为契约：飞书插件 import 期自注册——把自己注册进 ChannelRegistry 单例，
// 并把它的 10 条平台指令注册进 CommandRegistry 单例(channel='lark')。
// 这是「加平台 = 新增 plugins/xxx + 在 plugins/index.ts import 一行」的命门。

// 不桩 @aliyun/oss：src 下没有任何生产代码 import 它（`grep -rn "@aliyun/oss" src/`
// 只命中测试文件），桩了纯属往进程级 registry 里塞垃圾。
// @infrastructure/lane-router 必须桩，且这里曾经写着"不建连接、不起轮询"——那是错的：
// LaneRouter 构造函数里就是 `setInterval(() => this.poll(), 30_000)`，poll() 会
// `fetch(${registryUrl}/v1/routes)`，而该模块体直接 `new LaneRouter(...)`，所以只要
// 被 import（lark 插件经 file-pipeline / image-pipeline 确实 import 了）就立刻起轮询、
// 发真实网络请求。单测不该碰网络，失败还会被 poll 内部吞掉、变成看不见的挂起 timer。
//
// 抓真身这一步本身也会构造一次真 LaneRouter（无法回避：模块体就是 new），但这与
// qq-plugin.test.ts / file-pipeline.test.ts 的既有加载方式一致，不是新增行为。之所以
// 仍要抓真身，是因为 laneRouter 被 jieba / image-pipeline / file-pipeline 等多处生产
// 代码 import，留个只有 createClient 的假身会污染同进程后续文件。
//
// 真正的根治是把模块体的 `new` 改成惰性 getter（对齐 getRedisClient() 的形态），
// 让 import 不产生副作用——那属于 lark-service 组装根的重新设计，留给 Task B。
const realLaneRouter = { ...(await import('@infrastructure/lane-router')) };
mock.module('@infrastructure/lane-router', () => ({
    ...realLaneRouter,
    laneRouter: { createClient: () => ({ post: mock(async () => ({ data: {} })) }) },
}));
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
const { larkPlugin } = await import('./index');
const { getChannelRegistry } = await import('@inner/shared/channel');
const { getCommandRegistry } = await import('@inner/shared/rules');
const { getChannelRuntime } = await import('@plugins/runtime');

describe('lark 插件自注册', () => {
    it('import 即把 lark 插件注册进 ChannelRegistry 单例', () => {
        const reg = getChannelRegistry();
        expect(reg.has('lark')).toBe(true);
        expect(reg.get('lark')).toBe(larkPlugin);
        expect(larkPlugin.channel).toBe('lark');
    });

    it('import 即把 lark runtime 注册进 runtime registry', () => {
        const runtime = getChannelRuntime('lark');
        expect(runtime.channel).toBe('lark');
        expect(typeof runtime.registerHttpIngress).toBe('function');
        expect(typeof runtime.handleInboundLaneEnvelope).toBe('function');
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

afterAll(() => {
    mock.module('@inner/shared/cache', () => realSharedCache);
    mock.module('@infrastructure/lane-router', () => realLaneRouter);
});
