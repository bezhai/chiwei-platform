// 定时任务基座：清单内容、lane gate、挂任务、清单与本体自洽，以及"注册器真的接在入口
// 进程上"。
//
// 共同点是**失效的时候没有任何运行期症状**：
//
//   * 清单被改了 —— cron 表达式打错一位，任务照挂，只是换了个时间跑。
//   * lane gate 破了 —— 泳道部署会往写死的真实飞书群再发一遍日报、按小时全量覆写
//     共享的 lark_emoji 表。两边都不报错，prod 那份照常跑。
//   * 清单与本体接错了（run 的 key 拼错）—— 任务静默不挂，日志里连"少了一个"都看不
//     出来，只有到点没发日报才会有人发现。
//   * 注册器被从 index.ts 摘掉 —— 本文件其余每一条都还是绿的，因为它们测的是
//     startLarkSchedules 自己，而没人调用它。
//
// 所以最后一组读 index.ts / outbound.ts 的源码。这是本服务里唯一没法用行为断言覆盖
// 的一段（进程入口 import 即执行 main()），而它恰好是最容易在重构里被顺手删掉的一行。

import { afterAll, beforeEach, describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { context } from '@inner/shared/middleware';

import {
    LARK_SCHEDULES,
    startLarkSchedules,
    type CronJob,
    type CronScheduler,
    type LarkScheduleSlot,
} from './schedule';

// ---------------------------------------------------------------------------
// 调度器替身。绝不等时间流逝 —— 记下挂了什么，要跑就自己调。
// ---------------------------------------------------------------------------

interface FakeCron {
    schedule: CronScheduler;
    scheduled: { cron: string; run: () => void }[];
    stopped: string[];
    fire(cron: string): void;
}

function fakeCron(): FakeCron {
    const scheduled: { cron: string; run: () => void }[] = [];
    const stopped: string[] = [];
    const schedule: CronScheduler = (cron, run) => {
        scheduled.push({ cron, run });
        const job: CronJob = {
            stop: () => {
                stopped.push(cron);
            },
        };
        return job;
    };
    return {
        schedule,
        scheduled,
        stopped,
        fire(cron) {
            const job = scheduled.find((s) => s.cron === cron);
            if (!job) throw new Error(`nothing scheduled at ${cron}`);
            job.run();
        },
    };
}

const originalLane = process.env.LANE;

function onLane(lane: string | undefined): void {
    if (lane === undefined) delete process.env.LANE;
    else process.env.LANE = lane;
}

beforeEach(() => {
    onLane(undefined);
});

afterAll(() => {
    onLane(originalLane);
});

// 小清单，测挂载行为用；清单内容那组用真的 LARK_SCHEDULES。
const ALPHA: LarkScheduleSlot = { name: 'alpha', cron: '0 18 * * *', botName: 'tool' };
const BETA: LarkScheduleSlot = { name: 'beta', cron: '0 * * * *', botName: 'chiwei' };
/** 一格，配 `runs: { alpha }`。 */
const ONE: LarkScheduleSlot[] = [ALPHA];
/** 两格，配 `runs: { alpha, beta }`。 */
const TWO: LarkScheduleSlot[] = [ALPHA, BETA];

// ---------------------------------------------------------------------------

describe('清单内容', () => {
    // 这三样与拆分前逐条相同，改动它们都是**改线上行为**，不是改测试固定装置：cron 打
    // 错一位任务照挂、只是换了个时间跑；botName 换一个是换一套飞书凭据，任务照跑、发
    // 消息那一步才炸。先后不是契约（三个任务互不相干，cron 各跑各的）。
    it('三个任务的名字、cron 表达式、botName', () => {
        expect(LARK_SCHEDULES).toEqual([
            { name: 'daily-photo', cron: '0 18 * * *', botName: 'tool' },
            { name: 'daily-new-photo', cron: '30 19 * * *', botName: 'tool' },
            { name: 'emoji-sync', cron: '0 * * * *', botName: 'chiwei' },
        ]);
    });
});

describe('lane gate：非 prod 部署一个都不起', () => {
    // 副作用是全局的：日报往写死的真实飞书群发，emoji 每小时全量覆写共享表。泳道
    // 跑起来就是重复发群消息 + 写脏 prod 数据（理由与 channel-server 那份一字不差，
    // 也与 @inner/shared/lane-policy 的文件头一致）。
    it.each([undefined, 'prod'])('LANE=%s 视为 prod 部署，任务照挂', (lane) => {
        onLane(lane);
        const cron = fakeCron();

        const schedules = startLarkSchedules({
            runs: { alpha: async () => {} },
            schedule: cron.schedule,
            roster: ONE,
        });

        expect(schedules.running).toEqual(['alpha']);
        expect(cron.scheduled).toHaveLength(1);
    });

    it.each(['ppe-x', 'coe-y', 'blue'])('LANE=%s 不挂任何任务', (lane) => {
        onLane(lane);
        const cron = fakeCron();

        const schedules = startLarkSchedules({
            runs: { alpha: async () => {} },
            schedule: cron.schedule,
            roster: ONE,
        });

        expect(schedules.running).toEqual([]);
        expect(cron.scheduled).toEqual([]);
    });
});

describe('挂任务', () => {
    it('每一格都挂上去，cron 表达式原样交给调度器', () => {
        const cron = fakeCron();

        const schedules = startLarkSchedules({
            runs: { alpha: async () => {}, beta: async () => {} },
            schedule: cron.schedule,
            roster: TWO,
        });

        expect(schedules.running).toEqual(['alpha', 'beta']);
        expect(cron.scheduled.map((s) => s.cron)).toEqual(['0 18 * * *', '0 * * * *']);
    });

    // botName 决定这次执行拿哪套飞书凭据。丢了它，任务照跑、发消息那一步才炸。
    it('任务本体跑在带 botName 的上下文里，traceId 每次现生成', async () => {
        const cron = fakeCron();
        const seen: { botName: string; traceId: string }[] = [];

        startLarkSchedules({
            runs: {
                alpha: async () => {
                    seen.push({ botName: context.getBotName(), traceId: context.getTraceId() });
                },
            },
            schedule: cron.schedule,
            roster: ONE,
        });

        cron.fire('0 18 * * *');
        cron.fire('0 18 * * *');
        await Bun.sleep(0);

        expect(seen).toHaveLength(2);
        expect(seen.map((s) => s.botName)).toEqual(['tool', 'tool']);
        expect(seen[0]!.traceId).toBeTruthy();
        expect(seen[0]!.traceId).not.toBe(seen[1]!.traceId);
    });

    // index.ts 的 unhandledRejection 处理器是 process.exit(1)：任务抛出去的错没人接，
    // 一次 emoji 同步失败就会把持飞书长连的那个进程带走。
    it('任务抛错不外溢，调度器拿到的回调不会 reject', async () => {
        const cron = fakeCron();

        startLarkSchedules({
            runs: {
                alpha: async () => {
                    throw new Error('emoji api is down');
                },
            },
            schedule: cron.schedule,
            roster: ONE,
        });

        expect(() => cron.fire('0 18 * * *')).not.toThrow();
        await Bun.sleep(0);
    });

    it('stop() 停掉每一个挂上去的任务', () => {
        const cron = fakeCron();

        const schedules = startLarkSchedules({
            runs: { alpha: async () => {}, beta: async () => {} },
            schedule: cron.schedule,
            roster: TWO,
        });
        schedules.stop();

        expect(cron.stopped).toEqual(['0 18 * * *', '0 * * * *']);
    });
});

describe('清单与本体自洽：接错了要炸，不许静默不挂', () => {
    it('run 的 key 不在清单里 → 抛（拼错任务名的症状本来是任务永远不跑）', () => {
        const cron = fakeCron();

        expect(() =>
            startLarkSchedules({
                runs: { alhpa: async () => {} },
                schedule: cron.schedule,
                roster: ONE,
            }),
        ).toThrow(/alhpa/);
    });

    it('清单上的一格没人接本体 → 抛', () => {
        const cron = fakeCron();

        expect(() =>
            startLarkSchedules({
                runs: {},
                schedule: cron.schedule,
                roster: ONE,
            }),
        ).toThrow(/alpha/);
    });

    // 校验排在 lane gate 前面：泳道本来就不挂任务，接错了在那边永远看不见，等切到
    // prod 才第一次暴露 —— 而那时它已经是线上问题了。
    it('泳道部署也校验清单', () => {
        onLane('ppe-x');
        const cron = fakeCron();

        expect(() =>
            startLarkSchedules({
                runs: { alhpa: async () => {} },
                schedule: cron.schedule,
                roster: ONE,
            }),
        ).toThrow(/alhpa/);
    });
});

describe('装配：注册器接在入口进程上，且只接在入口进程上', () => {
    /** 注释掉不算接着。 */
    function code(entry: string): string {
        return readFileSync(resolve(import.meta.dir, entry), 'utf8')
            .split('\n')
            .filter((line) => {
                const trimmed = line.trim();
                return !trimmed.startsWith('//') && !trimmed.startsWith('*');
            })
            .join('\n');
    }

    it('index.ts 真的调了 startLarkSchedules', () => {
        const entry = code('index.ts');
        expect(entry).toContain("from './schedule'");
        expect(entry).toContain('startLarkSchedules(');
    });

    // reconcile 只在真进程起来的那一刻才跑，而 index.ts 一 import 就要连 PG / Redis /
    // MQ，本套测试跑不到它。所以"每一格在装配根真的有本体"这件事只能从源码上钉：漏掉
    // 一个的症状是 prod 起不来（reconcile 抛），但那时已经是线上问题了。
    it.each(LARK_SCHEDULES.map((slot) => slot.name))('%s 在装配根里真的接上了本体', (name) => {
        expect(code('index.ts')).toContain(`'${name}':`);
    });

    // 决策十的那张进程表没写 cron 归谁。它必须待在单副本的那个进程里：出站可以多副本，
    // 每个副本各起一份 cron 就是往写死的真实飞书群发 N 遍日报。
    it('outbound.ts 不碰定时任务', () => {
        const entry = code('outbound.ts');
        expect(entry).not.toContain('startLarkSchedules');
        expect(entry).not.toContain("from './schedule'");
    });
});
