import { afterAll, beforeEach, describe, expect, mock, test } from 'bun:test';

// ApplicationManager.initialize() 的装配契约：入站信封消费者只在泳道部署起，
// 且交给它的是「本进程能处理哪些 channel」。这里跑真实的 initialize()，只把它
// 够不着的重依赖（DB / MQ 连接 / channel runtime 初始化）换成 stub，lane 判定
// 走真实代码（读 process.env.LANE）。
//
// mock.module 在 bun 里是全局的，且 mock.restore() 不会撤销它：别的测试文件也在
// 用的模块（@plugins/runtime、@integrations/*），先把真身抓下来，afterAll 再注回
// 去，避免污染同一轮里后跑的测试文件。

const startInboundLaneConsumer = mock(async () => {});
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

const realInboundLaneConsumer = { ...(await import('@integrations/inbound-lane-consumer')) };
mock.module('@integrations/inbound-lane-consumer', () => ({
    ...realInboundLaneConsumer,
    startInboundLaneConsumer,
}));

const realPluginRuntime = { ...(await import('@plugins/runtime')) };
mock.module('@plugins/runtime', () => ({
    ...realPluginRuntime,
    initializeChannelRuntimes: async () => {},
    runChannelInitializers: async () => {},
    startChannelDirectIngresses,
}));

const { botDirectory } = await import('@inner/shared/bot');
const botDirectoryLoad = botDirectory.load;
botDirectory.load = async () => {};

const { ApplicationManager, createDefaultConfig } = await import('./application');

afterAll(() => {
    botDirectory.load = botDirectoryLoad;
    mock.module('@inner/shared/mq', () => realRabbitmq);
    mock.module('@integrations/inbound-lane-consumer', () => realInboundLaneConsumer);
    mock.module('@plugins/runtime', () => realPluginRuntime);
});

describe('ApplicationManager.initialize：入站信封消费者只在泳道部署起', () => {
    const originalLane = process.env.LANE;

    function initializeWithLane(lane: string | undefined): Promise<void> {
        if (lane === undefined) delete process.env.LANE;
        else process.env.LANE = lane;
        return new ApplicationManager(createDefaultConfig()).initialize();
    }

    beforeEach(() => {
        startChannelDirectIngresses.mockClear();
        startInboundLaneConsumer.mockClear();
    });

    afterAll(() => {
        if (originalLane === undefined) delete process.env.LANE;
        else process.env.LANE = originalLane;
    });

    test('LANE=ppe-x → 其余启动步骤照跑', async () => {
        await initializeWithLane('ppe-x');
        expect(startChannelDirectIngresses).toHaveBeenCalledTimes(1);
    });

    // 装配层交出去的是"本进程**能**处理哪些 channel"（注册面），而不是"拥有哪些"。
    // 拥有集合留给消费者按 dynamic config 现读，装配层不许在这里钉死。
    //
    // 飞书移交给 lark-service 之后注册面只剩 qq —— 这条断言同时钉住"没有第二个
    // runtime 悄悄回到 channel-server 里"。
    test('LANE=ppe-x → 消费者拿到的是已注册 runtime 的处理面', async () => {
        await initializeWithLane('ppe-x');
        expect(startInboundLaneConsumer).toHaveBeenCalledTimes(1);
        const [lane, , options] = startInboundLaneConsumer.mock.calls[0] as unknown as [
            string,
            unknown,
            { handles: string[]; loadOwnedChannels?: unknown },
        ];
        expect(lane).toBe('ppe-x');
        expect([...options.handles].sort()).toEqual(['qq']);
        expect(options.loadOwnedChannels).toBeUndefined();
    });

    // prod 不消费入站信封队列，它是投递方（§4.2）。
    test('LANE=prod → 不起入站信封消费者', async () => {
        await initializeWithLane('prod');
        expect(startInboundLaneConsumer).not.toHaveBeenCalled();
    });
});
