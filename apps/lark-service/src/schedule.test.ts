// 定时任务基座：清单对账、lane gate、挂任务、账本自洽，以及"注册器真的接在入口进程上"。
//
// 后面三组的共同点是**失效的时候没有任何运行期症状**：
//
//   * lane gate 破了 —— 泳道部署会往写死的真实飞书群再发一遍日报、按小时全量覆写
//     共享的 lark_emoji 表。两边都不报错，prod 那份照常跑。
//   * 账本接错了（run 的 key 拼错、槽位填了却忘了删 pendingIn）—— 任务静默不挂，
//     日志里连"少了一个"都看不出来，只有到点没发日报才会有人发现。
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
// 对面那份还活着的定时任务
// ---------------------------------------------------------------------------

const CHANNEL_SERVER_CRONTAB = resolve(
    import.meta.dir,
    '../../channel-server/src/infrastructure/crontab',
);

function readUpstream(relativePath: string): string {
    const path = resolve(CHANNEL_SERVER_CRONTAB, relativePath);
    try {
        return readFileSync(path, 'utf8');
    } catch {
        throw new Error(
            `读不到 ${path}。如果是 Task F 已经把 channel-server 的飞书代码删了，` +
                `那本文件的跨服务对账已经没有参照物 —— 确认三个槽位都填满之后把它删掉。`,
        );
    }
}

interface UpstreamTask {
    name: string;
    cron: string;
    botName: string;
}

/**
 * channel-server 那边有几个定时任务，是**数出来的**不是写死的：先从 services/index.ts
 * 取服务文件清单，再从每个文件里取 `@Crontab` 装饰器。对面新加一个而这边没跟上，
 * 上面那条对账就红。
 */
function upstreamTasks(): UpstreamTask[] {
    const modules = [...readUpstream('services/index.ts').matchAll(/from\s+'\.\/([\w-]+)'/g)].map(
        (m) => m[1]!,
    );
    return modules.flatMap((module) => {
        const source = readUpstream(`services/${module}.ts`);
        return [...source.matchAll(/@Crontab\(\s*'([^']*)'\s*,\s*\{([^}]*)\}/g)].map((match) => {
            const options = match[2]!;
            return {
                cron: match[1]!,
                name: /taskName:\s*'([^']*)'/.exec(options)![1]!,
                // 装饰器的 botName 有默认值（decorators.ts 里是 'chiwei'），不写也算数。
                botName: /botName:\s*'([^']*)'/.exec(options)?.[1] ?? 'chiwei',
            };
        });
    });
}

function byName<T extends { name: string }>(tasks: readonly T[]): T[] {
    return [...tasks].sort((a, b) => a.name.localeCompare(b.name));
}

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

// 小清单，测挂载行为用；对账那组用真的 LARK_SCHEDULES。
const ALPHA: LarkScheduleSlot = { name: 'alpha', cron: '0 18 * * *', botName: 'tool' };
const BETA: LarkScheduleSlot = { name: 'beta', cron: '0 * * * *', botName: 'chiwei' };
/** 一格，配 `runs: { alpha }`。 */
const ONE: LarkScheduleSlot[] = [ALPHA];
/** 两格，配 `runs: { alpha, beta }`。 */
const TWO: LarkScheduleSlot[] = [ALPHA, BETA];

// ---------------------------------------------------------------------------

describe('清单对账：定时任务与 channel-server 那份逐条对上', () => {
    // 先后不是契约（三个任务互不相干，cron 各跑各的），所以按名字排序比。
    it('任务名、cron 表达式、botName 逐条对上', () => {
        expect(byName(LARK_SCHEDULES).map(({ name, cron, botName }) => ({ name, cron, botName })))
            .toEqual(byName(upstreamTasks()));
    });

    // 槽位填没填**在行为上看不出来**：还欠着的槽位不挂任务，而"到点没跑"要等一整个
    // cron 周期才有人察觉。所以填充状态本身要有断言。三个都填满之后，`pendingIn` 这套
    // 脚手架在真清单上就没有用户了 —— 它连同跨服务对账一起在 Task F 删。
    it('三个槽位都搬过来了，没有还欠着的', () => {
        expect(LARK_SCHEDULES.map((slot) => [slot.name, slot.pendingIn])).toEqual([
            ['daily-photo', undefined],
            ['daily-new-photo', undefined],
            ['emoji-sync', undefined],
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
    it('只挂有本体的槽位，cron 表达式原样交给调度器', () => {
        const cron = fakeCron();

        const schedules = startLarkSchedules({
            runs: { beta: async () => {} },
            schedule: cron.schedule,
            roster: [{ ...ALPHA, pendingIn: 'D2' }, BETA],
        });

        expect(schedules.running).toEqual(['beta']);
        expect(cron.scheduled.map((s) => s.cron)).toEqual(['0 * * * *']);
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

describe('账本自洽：填错了要炸，不许静默不挂', () => {
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

    it('槽位既没有本体也没记着谁来填 → 抛', () => {
        const cron = fakeCron();

        expect(() =>
            startLarkSchedules({
                runs: {},
                schedule: cron.schedule,
                roster: ONE,
            }),
        ).toThrow(/alpha/);
    });

    it('槽位填上了却忘了删 pendingIn → 抛（账本还欠着，实际已经在跑）', () => {
        const cron = fakeCron();

        expect(() =>
            startLarkSchedules({
                runs: { alpha: async () => {} },
                schedule: cron.schedule,
                roster: [{ ...ALPHA, pendingIn: 'D2' }],
            }),
        ).toThrow(/alpha/);
    });

    // 校验排在 lane gate 前面：泳道本来就不挂任务，接错了在那边永远看不见，等切到
    // prod 才第一次暴露 —— 而那时它已经是线上问题了。
    it('泳道部署也校验账本', () => {
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

    // 账本对账（reconcile）只在真进程起来的那一刻才跑，而 index.ts 一 import 就要连
    // PG / Redis / MQ，本套测试跑不到它。所以"已经填上的槽位在装配根真的有本体"这件
    // 事只能从源码上钉：漏掉一个的症状是 prod 起不来（reconcile 抛），但那时已经是
    // 线上问题了。
    it.each(LARK_SCHEDULES.filter((slot) => !slot.pendingIn).map((slot) => slot.name))(
        '%s 在装配根里真的接上了本体',
        (name) => {
            expect(code('index.ts')).toContain(`'${name}':`);
        },
    );

    // 决策十的那张进程表没写 cron 归谁。它必须待在单副本的那个进程里：出站可以多副本，
    // 每个副本各起一份 cron 就是往写死的真实飞书群发 N 遍日报。
    it('outbound.ts 不碰定时任务', () => {
        const entry = code('outbound.ts');
        expect(entry).not.toContain('startLarkSchedules');
        expect(entry).not.toContain("from './schedule'");
    });
});
