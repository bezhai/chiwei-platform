import { afterAll, beforeEach, describe, expect, mock, test } from 'bun:test';

// ApplicationManager.initialize() 的装配契约。这里跑真实的 initialize()，只把它够不着
// 的重依赖（DB / MQ 连接 / channel runtime 初始化）换成 stub。
//
// mock.module 在 bun 里是全局的，且 mock.restore() 不会撤销它：别的测试文件也在用的
// 模块（@plugins/runtime、@inner/shared/mq），先把真身抓下来，afterAll 再注回去，避免
// 污染同一轮里后跑的测试文件。

const startChannelDirectIngresses = mock(async () => {});

// ./database 是 application.ts 独占的模块（按解析路径查过：src/startup/database.ts
// 全仓只有 application.ts 一个 import 方），进程里没有第二个消费者会被残缺
// namespace 打到，所以直接 mock、不还原。反过来说也不能还原：抓真身要 await
// import，那正好会把真实 DB 连接拖进来，等于白 mock。
mock.module('./database', () => ({
    DatabaseManager: {
        initialize: async () => {},
        close: async () => {},
    },
}));

const realRabbitmq = { ...(await import('@inner/shared/mq')) };
mock.module('@inner/shared/mq', () => ({
    ...realRabbitmq,
    rabbitmqClient: {
        connect: async () => {},
        declareTopology: async () => {},
        close: async () => {},
    },
}));

const realPluginRuntime = { ...(await import('@plugins/runtime')) };
mock.module('@plugins/runtime', () => ({
    ...realPluginRuntime,
    initializeChannelRuntimes: async () => {},
    runChannelInitializers: async () => {},
    startChannelDirectIngresses,
}));

const { channelRuntimes } = realPluginRuntime;

const { botDirectory } = await import('@inner/shared/bot');
const botDirectoryLoad = botDirectory.load;
botDirectory.load = async () => {};

const { ApplicationManager, createDefaultConfig } = await import('./application');

afterAll(() => {
    botDirectory.load = botDirectoryLoad;
    mock.module('@inner/shared/mq', () => realRabbitmq);
    mock.module('@plugins/runtime', () => realPluginRuntime);
});

describe('ApplicationManager.initialize', () => {
    const originalLane = process.env.LANE;

    function initializeWithLane(lane: string | undefined): Promise<void> {
        if (lane === undefined) delete process.env.LANE;
        else process.env.LANE = lane;
        return new ApplicationManager(createDefaultConfig()).initialize();
    }

    beforeEach(() => {
        startChannelDirectIngresses.mockClear();
    });

    afterAll(() => {
        if (originalLane === undefined) delete process.env.LANE;
        else process.env.LANE = originalLane;
    });

    // 泳道交接的接收端是一条 HTTP 路由，跟着各 runtime 的 registerHttpIngress 走，
    // 启动序列因此不再按泳道分叉：prod 和泳道跑的是同一串步骤。
    test.each(['prod', 'ppe-x', undefined])('LANE=%s → 启动步骤照跑', async (lane) => {
        await initializeWithLane(lane);
        expect(startChannelDirectIngresses).toHaveBeenCalledTimes(1);
    });

    // 飞书移交给 lark-service 之后本服务的注册面只剩 qq。这条断言钉住"没有第二个
    // runtime 悄悄回到 channel-server 里"。
    test('注册面只有 qq 一个 channel runtime', async () => {
        await initializeWithLane('prod');
        expect(channelRuntimes().map((runtime) => runtime.channel).sort()).toEqual(['qq']);
    });
});
