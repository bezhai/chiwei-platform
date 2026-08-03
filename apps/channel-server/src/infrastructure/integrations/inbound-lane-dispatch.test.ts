import { afterAll, describe, it, expect, beforeEach, mock } from 'bun:test';

// dispatchInboundIfNeeded 是 handlers 决策点的组装函数：读 flag → 算 lane →
// 非本进程 lane 投 inbound_lane.{lane} 并返回 true(已分流，handler 应 return)；
// 本地处理返回 false(handler 继续走现状链路)。
//
// 这里把 flag / resolveLane / publish 全部注入，确定性验证三条分叉 + 零回归红线
// （flag off 完全不碰 resolveLane / publish）。

const publishCalls: unknown[] = [];
// 整模块替换会顶掉 inboundLaneQueueName / inboundDedupeKey / publishInboundLane 等
// 导出（inbound-lane.test.ts、inbound-lane-consumer.ts 都在用），先抓真身。
const realInboundLane = { ...(await import('./inbound-lane')) };
mock.module('./inbound-lane', () => ({
    ...realInboundLane,
    dispatchToInboundLane: async (env: unknown) => {
        publishCalls.push(env);
    },
}));

let flagValue = false;
// 同理：inbound-lane-flag.test.ts 用的是同模块的 readInboundLaneDispatchFlag。
const realInboundLaneFlag = { ...(await import('./inbound-lane-flag')) };
mock.module('./inbound-lane-flag', () => ({
    ...realInboundLaneFlag,
    isInboundLaneDispatchEnabled: async () => flagValue,
}));

let resolveLaneImpl: (
    channel: string,
    bot: string,
    commonConversationId: string | undefined,
) => Promise<string> = async () => 'prod';
// 同 lane-bindings.route.test.ts：整模块替换会顶掉同模块其他导出，先抓真身。
const realLaneBinding = { ...(await import('@inner/shared/lane-binding')) };
mock.module('@inner/shared/lane-binding', () => ({
    ...realLaneBinding,
    getLaneBindingResolver: () => ({
        resolveLane: (channel: string, bot: string, commonConversationId: string | undefined) =>
            resolveLaneImpl(channel, bot, commonConversationId),
    }),
}));

// bun 的 mock.module 注册表进程级全局、mock.restore() 不撤销：三个 mock 都必须
// 在本文件跑完后放回真身，否则后续文件里的生产代码会一直吃到这些桩。
afterAll(() => {
    mock.module('./inbound-lane', () => realInboundLane);
    mock.module('./inbound-lane-flag', () => realInboundLaneFlag);
    mock.module('@inner/shared/lane-binding', () => realLaneBinding);
});

const { dispatchInboundIfNeeded } = await import('./inbound-lane-dispatch');

const baseInput = {
    currentLane: 'prod',
    channel: 'lark',
    botGlobalId: 'chiwei',
    commonConversationId: '018f-chat',
    eventType: 'im.message.receive_v1',
    globalMessageId: 'gmid-1',
    traceId: 'trace-1',
    params: { message: { chat_id: 'oc_1' } },
};

describe('dispatchInboundIfNeeded', () => {
    beforeEach(() => {
        publishCalls.length = 0;
        flagValue = false;
        resolveLaneImpl = async () => 'prod';
    });

    it('flag off → 返回 false(本地处理)，完全不算 lane、不投 MQ', async () => {
        let resolveLaneCalled = false;
        resolveLaneImpl = async () => {
            resolveLaneCalled = true;
            return 'ppe-foo';
        };

        const dispatched = await dispatchInboundIfNeeded(baseInput);

        expect(dispatched).toBe(false);
        expect(resolveLaneCalled).toBe(false);
        expect(publishCalls.length).toBe(0);
    });

    it('flag on + lane==本进程 → 返回 false(本地)，不投 MQ', async () => {
        flagValue = true;
        resolveLaneImpl = async () => 'prod';

        const dispatched = await dispatchInboundIfNeeded(baseInput);

        expect(dispatched).toBe(false);
        expect(publishCalls.length).toBe(0);
    });

    it('flag on + lane!=本进程 → 投 inbound_lane.{lane} 并返回 true(已分流)', async () => {
        flagValue = true;
        resolveLaneImpl = async () => 'ppe-foo';

        const dispatched = await dispatchInboundIfNeeded(baseInput);

        expect(dispatched).toBe(true);
        expect(publishCalls.length).toBe(1);
        expect(publishCalls[0]).toEqual({
            channel: 'lark',
            event_type: 'im.message.receive_v1',
            global_message_id: 'gmid-1',
            trace_id: 'trace-1',
            lane: 'ppe-foo',
            bot_name: 'chiwei',
            params: baseInput.params,
        });
    });

    it('passes commonConversationId into lane resolution for chat binding', async () => {
        flagValue = true;
        let seenConversationId: string | undefined;
        resolveLaneImpl = async (_channel, _bot, commonConversationId) => {
            seenConversationId = commonConversationId;
            return 'prod';
        };

        await dispatchInboundIfNeeded(baseInput);

        expect(seenConversationId).toBe('018f-chat');
    });
});
