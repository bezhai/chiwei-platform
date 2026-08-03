import { afterAll, beforeEach, describe, expect, mock, test } from 'bun:test';

// crontab 的副作用是全局的（daily-photo 往写死的真实飞书群发消息、emoji 每小时
// 全量覆写共享表），所以 initialize() 只在 prod 部署起 crontab，泳道部署跳过。
// 这里跑真实的 initialize()，只把它够不着的重依赖（DB / MQ 连接 / channel runtime
// 初始化）换成 stub，lane 判定走真实代码（读 process.env.LANE）。
//
// mock.module 在 bun 里是全局的，且 mock.restore() 不会撤销它：别的测试文件也在
// 用的模块（@plugins/runtime、@integrations/*），先把真身抓下来，afterAll 再注回
// 去，避免污染同一轮里后跑的测试文件。

const initializeCrontabs = mock(() => {});
const startInboundLaneConsumer = mock(async () => {});
const startChannelDirectIngresses = mock(async () => {});

// 下面两个是 application.ts 独占的模块（按解析路径查过：src/startup/database.ts
// 和 src/infrastructure/crontab/index.ts 全仓只有 application.ts 一个 import 方，
// crontab 的其他代码走 ./registry 和 @crontab/decorators），进程里没有第二个消费者
// 会被残缺 namespace 打到，所以直接 mock、不还原。反过来说也不能还原：抓真身要
// await import，那正好会把 crontab 的一堆服务模块和真实 DB 连接拖进来，等于白 mock。
mock.module('./database', () => ({
    DatabaseManager: {
        initialize: async () => {},
        close: async () => {},
    },
}));
mock.module('@crontab/index', () => ({ initializeCrontabs }));

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

describe('ApplicationManager.initialize：crontab 只在 prod 部署起', () => {
    const originalLane = process.env.LANE;

    function initializeWithLane(lane: string | undefined): Promise<void> {
        if (lane === undefined) delete process.env.LANE;
        else process.env.LANE = lane;
        return new ApplicationManager(createDefaultConfig()).initialize();
    }

    beforeEach(() => {
        initializeCrontabs.mockClear();
        startChannelDirectIngresses.mockClear();
    });

    afterAll(() => {
        if (originalLane === undefined) delete process.env.LANE;
        else process.env.LANE = originalLane;
    });

    test('LANE 未注入（等价 prod）→ 起 crontab', async () => {
        await initializeWithLane(undefined);
        expect(initializeCrontabs).toHaveBeenCalledTimes(1);
    });

    test('LANE=prod → 起 crontab', async () => {
        await initializeWithLane('prod');
        expect(initializeCrontabs).toHaveBeenCalledTimes(1);
    });

    test('LANE=ppe-x → 不起 crontab（其余启动步骤照跑）', async () => {
        await initializeWithLane('ppe-x');
        expect(initializeCrontabs).not.toHaveBeenCalled();
        expect(startChannelDirectIngresses).toHaveBeenCalledTimes(1);
    });

    test('LANE=coe-y → 不起 crontab', async () => {
        await initializeWithLane('coe-y');
        expect(initializeCrontabs).not.toHaveBeenCalled();
    });
});
