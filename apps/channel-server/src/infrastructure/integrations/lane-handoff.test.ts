import { afterAll, describe, it, expect, beforeEach, mock } from 'bun:test';

// handlers 决策点的组装函数，两步：resolveInboundLaneHandoff 读 flag → 算 lane →
// 该交出去就返回备好的信封（本地处理返回 null，handler 继续走现状链路）；
// handOffToLane 才真的把信封打给目标泳道。
//
// 判定与投递分开，是因为它们分处投影锁的两侧（见被测文件顶部）：判定要用投影产出的
// commonConversationId，只能在锁里；投递是同步等对端处理完，只能在锁外。所以这里也分开
// 验：判定这一组**一次请求都不该发**。
//
// flag / resolveLane / laneRouter 全部注入或替换，确定性验证分叉 + 零回归红线
// （flag off 完全不碰 resolveLane / 不发请求）。

import { context } from '@middleware/context';
import { register } from '@middleware/metrics';

interface FetchCall {
    service: string;
    path: string;
    init: RequestInit | undefined;
    // 发出请求那一刻 context 里的值。LaneRouter 就是从这三个值算出
    // x-ctx-lane / X-Trace-Id / X-App-Name 的，所以断言它们等于断言 header。
    lane: string | undefined;
    traceId: string;
    botName: string | undefined;
}

const fetchCalls: FetchCall[] = [];
let fetchImpl: (service: string, path: string, init?: RequestInit) => Promise<Response> = async () =>
    okResponse('ppe-foo');

function okResponse(handledByLane: string): Response {
    return new Response(JSON.stringify({ success: true, handled_by_lane: handledByLane }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
    });
}

// laneRouter 真身在模块作用域 new LaneRouter(...)，构造即打 lite-registry 并起 30s 轮询。
// bun 的 mock.module 是整模块替换 + 进程级全局（mock.restore() 撤不掉），所以先抓真身、
// afterAll 放回去，否则后跑的文件会拿到只有 fetch 的假身。
const realLaneRouter = { ...(await import('@infrastructure/lane-router')) };
mock.module('@infrastructure/lane-router', () => ({
    ...realLaneRouter,
    laneRouter: {
        fetch: (service: string, path: string, init?: RequestInit) => {
            fetchCalls.push({
                service,
                path,
                init,
                lane: context.getLane(),
                traceId: context.getTraceId(),
                botName: context.getBotName(),
            });
            return fetchImpl(service, path, init);
        },
    },
}));

let flagValue = false;
// 同理：lane-dispatch-flag.test.ts 用的是同模块的 readInboundLaneDispatchFlag。
const realInboundLaneFlag = { ...(await import('./lane-dispatch-flag')) };
mock.module('./lane-dispatch-flag', () => ({
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

const SECRET = 'inner-secret-under-test';
const originalSecret = process.env.INNER_HTTP_SECRET;
process.env.INNER_HTTP_SECRET = SECRET;

afterAll(() => {
    mock.module('@infrastructure/lane-router', () => realLaneRouter);
    mock.module('./lane-dispatch-flag', () => realInboundLaneFlag);
    mock.module('@inner/shared/lane-binding', () => realLaneBinding);
    if (originalSecret === undefined) Reflect.deleteProperty(process.env, 'INNER_HTTP_SECRET');
    else process.env.INNER_HTTP_SECRET = originalSecret;
});

const { resolveInboundLaneHandoff, handOffToLane, LANE_HANDOFF_TIMEOUT_MS } =
    await import('./lane-handoff');
const { QQ_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS } = await import('@plugins/qq/common-projector');

const baseInput = {
    handedOff: false,
    currentLane: 'prod',
    channel: 'qq',
    botGlobalId: 'chiwei',
    commonConversationId: '018f-chat',
    eventType: 'qq.message.receive',
    globalMessageId: 'gmid-1',
    traceId: 'trace-1',
    params: { messageId: 'msg_1' },
};

const envelope = {
    channel: 'qq',
    event_type: 'qq.message.receive',
    global_message_id: 'gmid-1',
    trace_id: 'trace-1',
    lane: 'ppe-foo',
    bot_name: 'chiwei',
    handed_off: true as const,
    params: baseInput.params,
};

// 指标快照：`{channel}/{target_lane}/{outcome}` → 计数。断言用前后差值，避免依赖
// 其它用例留下的累计值。
async function handoffCounts(): Promise<Record<string, number>> {
    const metric = register.getSingleMetric('lane_handoff_total');
    if (!metric) return {};
    const snapshot = (await metric.get()) as unknown as {
        values: { labels: Record<string, string>; value: number }[];
    };
    const out: Record<string, number> = {};
    for (const v of snapshot.values) {
        out[`${v.labels.channel}/${v.labels.target_lane}/${v.labels.outcome}`] = v.value;
    }
    return out;
}

async function captureLogs(run: () => Promise<void>): Promise<string[]> {
    const lines: string[] = [];
    const info = console.info;
    const warn = console.warn;
    console.info = (...args: unknown[]) => void lines.push(args.map(String).join(' '));
    console.warn = (...args: unknown[]) => void lines.push(args.map(String).join(' '));
    try {
        await run();
    } finally {
        console.info = info;
        console.warn = warn;
    }
    return lines;
}

// 接收端拿到信封后重走投影，第一件事是抢同一条 qq_message_id 的投影锁，最长会在锁上
// 排 QQ_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS 才轮到自己开始干活。投递方的超时如果小于
// 这个窗口，只要接收端在排队（正常并发，或前一个持有者 release 失败留下的残留租约 ——
// 那把锁 release 失败是被吞掉的，租约 120s），投递方必先超时。而交接不重试、QQ 那侧
// 早已收下，这条消息就此静默消失。
describe('交接超时必须盖得住接收端的锁等待窗口', () => {
    it('投递超时严格大于投影锁的等待窗口', () => {
        expect(LANE_HANDOFF_TIMEOUT_MS).toBeGreaterThan(QQ_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS);
    });
});

describe('resolveInboundLaneHandoff', () => {
    beforeEach(() => {
        fetchCalls.length = 0;
        flagValue = false;
        resolveLaneImpl = async () => 'prod';
        fetchImpl = async () => okResponse('ppe-foo');
    });

    it('flag off → 返回 null(本地处理)，完全不算 lane、不发请求', async () => {
        let resolveLaneCalled = false;
        resolveLaneImpl = async () => {
            resolveLaneCalled = true;
            return 'ppe-foo';
        };

        const handoff = await resolveInboundLaneHandoff(baseInput);

        expect(handoff).toBeNull();
        expect(resolveLaneCalled).toBe(false);
        expect(fetchCalls.length).toBe(0);
    });

    it('flag on + lane==本进程 → 返回 null(本地)，不发请求', async () => {
        flagValue = true;
        resolveLaneImpl = async () => 'prod';

        const handoff = await resolveInboundLaneHandoff(baseInput);

        expect(handoff).toBeNull();
        expect(fetchCalls.length).toBe(0);
    });

    it('flag on + lane!=本进程 → 备好带 handed_off 的信封，但判定这一步不发请求', async () => {
        flagValue = true;
        resolveLaneImpl = async () => 'ppe-foo';

        const handoff = await resolveInboundLaneHandoff(baseInput);

        expect(handoff).toEqual(envelope);
        // 投递是调用方在锁外的事，判定本身一条请求都不发。
        expect(fetchCalls.length).toBe(0);
    });

    it('已交接的信封 → 返回 null，不再算 lane、不再交接（自投循环的阻断点）', async () => {
        flagValue = true;
        let resolveLaneCalled = false;
        resolveLaneImpl = async () => {
            resolveLaneCalled = true;
            return 'ppe-foo';
        };

        const handoff = await resolveInboundLaneHandoff({ ...baseInput, handedOff: true });

        expect(handoff).toBeNull();
        expect(resolveLaneCalled).toBe(false);
        expect(fetchCalls.length).toBe(0);
    });

    it('passes commonConversationId into lane resolution for chat binding', async () => {
        flagValue = true;
        let seenConversationId: string | undefined;
        resolveLaneImpl = async (_channel, _bot, commonConversationId) => {
            seenConversationId = commonConversationId;
            return 'prod';
        };

        await resolveInboundLaneHandoff(baseInput);

        expect(seenConversationId).toBe('018f-chat');
    });
});

describe('handOffToLane', () => {
    beforeEach(() => {
        fetchCalls.length = 0;
        fetchImpl = async () => okResponse('ppe-foo');
    });

    it('打到目标渠道的泳道信封端点，带内网 Bearer 和完整信封', async () => {
        await handOffToLane(envelope);

        expect(fetchCalls.length).toBe(1);
        const call = fetchCalls[0];
        expect(call.service).toBe('channel-server');
        expect(call.path).toBe('/api/internal/qq/lane-inbound');
        expect(call.init?.method).toBe('POST');
        expect((call.init?.headers as Record<string, string>).Authorization).toBe(
            `Bearer ${SECRET}`,
        );
        expect(JSON.parse(call.init?.body as string)).toEqual(envelope);
    });

    it('以**目标**泳道建立 context（LaneRouter 据此注入 x-ctx-lane），不是本进程泳道', async () => {
        // 本进程处在另一条泳道的 context 里，交接仍必须按信封的 lane 选路。
        await context.run(context.createContext('another-bot', 'trace-outer', 'ppe-current'), () =>
            handOffToLane(envelope),
        );

        expect(fetchCalls[0].lane).toBe('ppe-foo');
        expect(fetchCalls[0].traceId).toBe('trace-1');
        expect(fetchCalls[0].botName).toBe('chiwei');
    });

    it('请求带显式超时上限（AbortSignal）', async () => {
        await handOffToLane(envelope);

        expect(fetchCalls[0].init?.signal).toBeInstanceOf(AbortSignal);
    });

    it('对端非 2xx → 抛（fail-loud，绝不当成已送达）', async () => {
        fetchImpl = async () => new Response('lane is drowning', { status: 503 });

        await expect(handOffToLane(envelope)).rejects.toThrow(/503/);
    });

    it('连接失败 → 抛', async () => {
        fetchImpl = async () => {
            throw new Error('connect ECONNREFUSED');
        };

        await expect(handOffToLane(envelope)).rejects.toThrow(/ECONNREFUSED/);
    });

    it('超时 → 抛', async () => {
        // 真的挂住，靠调用方给的超时上限把它掐掉。
        fetchImpl = (_service, _path, init) =>
            new Promise((_resolve, reject) => {
                init?.signal?.addEventListener('abort', () =>
                    reject((init.signal as AbortSignal).reason),
                );
            });

        await expect(handOffToLane(envelope, 20)).rejects.toThrow(/timed out/);
    });

    it('对端回报的 lane == 目标泳道 → 记「送达泳道」', async () => {
        const before = await handoffCounts();
        fetchImpl = async () => okResponse('ppe-foo');

        const logs = await captureLogs(() => handOffToLane(envelope));

        const after = await handoffCounts();
        expect((after['qq/ppe-foo/lane'] ?? 0) - (before['qq/ppe-foo/lane'] ?? 0)).toBe(1);
        expect(after['qq/ppe-foo/fallback'] ?? 0).toBe(before['qq/ppe-foo/fallback'] ?? 0);
        expect(logs.join('\n')).toContain('handled_by_lane=ppe-foo');
    });

    it('对端回报 prod（泳道 Service 不存在，sidecar 打回 prod）→ 记「落回 prod」，与送达可区分', async () => {
        const before = await handoffCounts();
        fetchImpl = async () => okResponse('prod');

        const logs = await captureLogs(() => handOffToLane(envelope));

        const after = await handoffCounts();
        expect((after['qq/ppe-foo/fallback'] ?? 0) - (before['qq/ppe-foo/fallback'] ?? 0)).toBe(1);
        expect(after['qq/ppe-foo/lane'] ?? 0).toBe(before['qq/ppe-foo/lane'] ?? 0);
        const text = logs.join('\n');
        expect(text).toContain('handled_by_lane=prod');
        expect(text).toContain('fell back');
    });

    it('投递失败也记进指标（outcome=error），失败不是无声的', async () => {
        const before = await handoffCounts();
        fetchImpl = async () => new Response('nope', { status: 500 });

        await expect(handOffToLane(envelope)).rejects.toThrow();

        const after = await handoffCounts();
        expect((after['qq/ppe-foo/error'] ?? 0) - (before['qq/ppe-foo/error'] ?? 0)).toBe(1);
    });
});
