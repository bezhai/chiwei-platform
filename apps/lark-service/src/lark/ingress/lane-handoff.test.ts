// 交接的投递侧。判断（该走哪条泳道）在 chooseInboundLane，投递（打哪个端点、怎么算
// 失败）在 handOffToInboundLane —— 两件事分开测，因为一个是纯决策、一个是 HTTP 的
// 具体形态。

import { describe, expect, it, spyOn } from 'bun:test';
import { context } from '@inner/shared/middleware';

import { register } from '../../server/metrics';
import { LARK_MESSAGE_LOCK } from '../projection/message-lock';
import {
    LANE_HANDOFF_TIMEOUT_MS,
    chooseInboundLane,
    handOffToInboundLane,
    type LaneHandoffFetcher,
} from './lane-handoff';
import { LANE_INBOUND_PATH } from './lane-inbound';
import type { InboundLaneEnvelope } from './lane-envelope';

// 指标快照：`{channel}/{target_lane}/{outcome}` → 计数。断言用前后差值，别的用例也
// 会打点到同一个 registry。
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

function envelope(overrides: Partial<InboundLaneEnvelope> = {}): InboundLaneEnvelope {
    return {
        channel: 'lark',
        event_type: 'im.message.receive_v1',
        global_message_id: 'cm_1',
        trace_id: 'trace-1',
        lane: 'ppe-x',
        bot_name: 'chiwei',
        params: { message: { message_id: 'om_1' } },
        handed_off: true,
        ...overrides,
    };
}

// 接收端收到信封后重走投影，第一件事就是抢同一条 om_id 的投影锁，最长能在锁上排
// LARK_MESSAGE_LOCK.waitTimeoutMs 才轮到自己开始干活。投递方的超时如果小于这个窗口，
// 只要接收端在排队（正常并发，或前一个持有者 release 失败留下的残留租约），投递方就
// 必定先超时 —— 而交接不重试、飞书早已应答，这条消息就此静默消失。
describe('交接超时必须盖得住接收端的锁等待窗口', () => {
    it('投递超时严格大于投影锁的等待窗口', () => {
        expect(LANE_HANDOFF_TIMEOUT_MS).toBeGreaterThan(LARK_MESSAGE_LOCK.waitTimeoutMs);
    });
});

describe('chooseInboundLane', () => {
    it('开关关着时不算泳道，本地处理', async () => {
        let asked = 0;
        const choice = await chooseInboundLane({
            handedOff: false,
            dispatchEnabled: false,
            currentLane: 'prod',
            laneOf: async () => {
                asked += 1;
                return 'ppe-x';
            },
        });

        expect(choice).toEqual({ handOff: false, lane: 'prod' });
        expect(asked).toBe(0);
    });

    // 泳道部署拿到的信封已经被 prod 判过一次，信封里的 lane 才是权威。再判一次会
    // 在绑定刚改过时把消息二次转投到别的泳道。
    it('本进程不是 prod 时不再判，直接本地处理', async () => {
        let asked = 0;
        const choice = await chooseInboundLane({
            handedOff: false,
            dispatchEnabled: true,
            currentLane: 'ppe-x',
            laneOf: async () => {
                asked += 1;
                return 'ppe-y';
            },
        });

        expect(choice).toEqual({ handOff: false, lane: 'ppe-x' });
        expect(asked).toBe(0);
    });

    it('算出来就是本进程的泳道时本地处理，绝不投给自己', async () => {
        const choice = await chooseInboundLane({
            handedOff: false,
            dispatchEnabled: true,
            currentLane: 'prod',
            laneOf: async () => 'prod',
        });

        expect(choice).toEqual({ handOff: false, lane: 'prod' });
    });

    it('算出来是别的泳道时交出去', async () => {
        const choice = await chooseInboundLane({
            handedOff: false,
            dispatchEnabled: true,
            currentLane: 'prod',
            laneOf: async () => 'ppe-x',
        });

        expect(choice).toEqual({ handOff: true, lane: 'ppe-x' });
    });

    // 泳道的 Service 不存在时 sidecar 把请求打回 prod 自己，于是 currentLane 又是
    // 'prod'、绑定又指向那条泳道。没有这个分支就是无限自投。
    it('已经交接过的信封不再判泳道，哪怕本进程是 prod', async () => {
        let asked = 0;
        const choice = await chooseInboundLane({
            handedOff: true,
            dispatchEnabled: true,
            currentLane: 'prod',
            laneOf: async () => {
                asked += 1;
                return 'ppe-x';
            },
        });

        expect(choice).toEqual({ handOff: false, lane: 'prod' });
        expect(asked).toBe(0);
    });
});


describe('handOffToInboundLane', () => {
    const SECRET = 'inner-secret';

    /**
     * LaneRouter 的替身。**必须在被调用的那一刻从 AsyncLocal context 读 lane**：真的
     * LaneRouter 就是这么拼 `x-ctx-lane` 的，换成"构造时读一次"就测不出投递方有没有
     * 把上下文切到目标泳道。
     */
    function router(respond: (init?: RequestInit) => Promise<Response>) {
        const calls: Array<{
            service: string;
            path: string;
            headers: Record<string, string>;
            body: unknown;
            signal?: AbortSignal | null;
        }> = [];
        const fetcher: LaneHandoffFetcher = {
            fetch: async (service, path, init) => {
                const lane = context.getLane();
                calls.push({
                    service,
                    path,
                    headers: {
                        ...(lane ? { 'x-ctx-lane': lane } : {}),
                        ...((init?.headers as Record<string, string>) ?? {}),
                    },
                    body: init?.body ? JSON.parse(init.body as string) : undefined,
                    signal: init?.signal as AbortSignal | undefined,
                });
                return respond(init);
            },
        };
        return { calls, fetcher };
    }

    const handled = (lane: string) =>
        new Response(JSON.stringify({ success: true, handled_by_lane: lane }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
        });

    const ok = () => router(async () => handled('ppe-x'));

    it('把信封 POST 到 lark-service 的交接端点，带内网 Bearer 口令', async () => {
        const { calls, fetcher } = ok();

        await handOffToInboundLane({ fetcher, innerSecret: SECRET }, envelope());

        expect(calls).toHaveLength(1);
        expect(calls[0]!.service).toBe('lark-service');
        expect(calls[0]!.path).toBe(LANE_INBOUND_PATH);
        expect(calls[0]!.headers.Authorization).toBe(`Bearer ${SECRET}`);
    });

    it('信封原样序列化，「已交接」标记一起过去', async () => {
        const { calls, fetcher } = ok();
        const env = envelope();

        await handOffToInboundLane({ fetcher, innerSecret: SECRET }, env);

        expect(calls[0]!.body).toEqual(env as unknown as Record<string, unknown>);
    });

    // 这一条是选路的全部：sidecar 只看 x-ctx-lane。投递方在 prod 上跑，本进程的上下文
    // 说的是 prod —— 照它发就是把消息打给自己。语义与 qq-gateway 的 selfLane 相反。
    it('x-ctx-lane 是目标泳道，不是本进程的泳道', async () => {
        const { calls, fetcher } = ok();

        await context.run(context.createContext('trace-outer', { lane: 'prod' }), () =>
            handOffToInboundLane({ fetcher, innerSecret: SECRET }, envelope({ lane: 'ppe-x' })),
        );

        expect(calls[0]!.headers['x-ctx-lane']).toBe('ppe-x');
    });

    // 投递方不重试，"投出去了"必须等于"对面处理完了"。非 2xx 当成功就是一条静默丢失。
    it('非 2xx 一律抛错，且说清楚是哪条消息、哪条泳道', async () => {
        const { fetcher } = router(async () => new Response('boom', { status: 500 }));

        await expect(
            handOffToInboundLane({ fetcher, innerSecret: SECRET }, envelope()),
        ).rejects.toThrow(/500.*ppe-x.*cm_1|ppe-x.*cm_1.*500/s);
    });

    it('4xx 同样算投递失败', async () => {
        const { fetcher } = router(async () => new Response('nope', { status: 400 }));

        await expect(
            handOffToInboundLane({ fetcher, innerSecret: SECRET }, envelope()),
        ).rejects.toThrow(/400/);
    });

    it('连接失败抛错', async () => {
        const { fetcher } = router(async () => {
            throw new Error('ECONNREFUSED');
        });

        await expect(
            handOffToInboundLane({ fetcher, innerSecret: SECRET }, envelope()),
        ).rejects.toThrow(/ECONNREFUSED/);
    });

    // 必须有显式上限：接收端是同步处理，卡住的话没有超时就是投递方跟着一起卡死。
    it('超时抛错，而且请求真的被取消掉', async () => {
        let aborted = false;
        const { fetcher } = router(
            (init) =>
                new Promise<Response>((_resolve, reject) => {
                    init?.signal?.addEventListener('abort', () => {
                        aborted = true;
                        reject((init.signal as AbortSignal).reason);
                    });
                }),
        );

        await expect(
            handOffToInboundLane({ fetcher, innerSecret: SECRET, timeoutMs: 5 }, envelope()),
        ).rejects.toThrow(/timed out after 5ms/);
        expect(aborted).toBe(true);
    });

    // 落回 prod 与送达泳道在投递方眼里一模一样（都是 200），所以必须靠接收端回报的
    // 泳道分辨。分不出来的话，"泳道漏部署了"这件事永远查不出来。
    describe('投递结果可观测', () => {
        it('送达目标泳道时记一条 info，不告警', async () => {
            const info = spyOn(console, 'info').mockImplementation(() => {});
            const warn = spyOn(console, 'warn').mockImplementation(() => {});
            try {
                const { fetcher } = router(async () => handled('ppe-x'));
                await handOffToInboundLane({ fetcher, innerSecret: SECRET }, envelope());

                expect(info.mock.calls.flat().join(' ')).toContain('ppe-x');
                expect(warn).not.toHaveBeenCalled();
            } finally {
                info.mockRestore();
                warn.mockRestore();
            }
        });

        it('落回 prod 时告警，并说出信封本来要去哪条泳道', async () => {
            const warn = spyOn(console, 'warn').mockImplementation(() => {});
            try {
                const { fetcher } = router(async () => handled('prod'));
                await handOffToInboundLane({ fetcher, innerSecret: SECRET }, envelope());

                const said = warn.mock.calls.flat().join(' ');
                expect(said).toContain('ppe-x');
                expect(said).toContain('prod');
            } finally {
                warn.mockRestore();
            }
        });

        // 日志只能人去 grep，告不了警。三种结局各记一个 outcome，"绑定指向的泳道没
        // 部署"才有可能变成一条告警规则而不是等人发现改动没生效。
        it('三种结局各记一次 lane_handoff_total', async () => {
            const info = spyOn(console, 'info').mockImplementation(() => {});
            const warn = spyOn(console, 'warn').mockImplementation(() => {});
            try {
                const before = await handoffCounts();

                await handOffToInboundLane(
                    { fetcher: router(async () => handled('ppe-x')).fetcher, innerSecret: SECRET },
                    envelope(),
                );
                await handOffToInboundLane(
                    { fetcher: router(async () => handled('prod')).fetcher, innerSecret: SECRET },
                    envelope(),
                );
                await expect(
                    handOffToInboundLane(
                        {
                            fetcher: router(async () => new Response('nope', { status: 503 }))
                                .fetcher,
                            innerSecret: SECRET,
                        },
                        envelope(),
                    ),
                ).rejects.toThrow();

                const after = await handoffCounts();
                const rose = (key: string) => (after[key] ?? 0) - (before[key] ?? 0);
                expect(rose('lark/ppe-x/lane')).toBe(1);
                expect(rose('lark/ppe-x/fallback')).toBe(1);
                expect(rose('lark/ppe-x/error')).toBe(1);
            } finally {
                info.mockRestore();
                warn.mockRestore();
            }
        });
    });
});
