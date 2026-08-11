// 飞书专属定时任务的基座：一份清单、一道 lane gate、一个把它们挂上去的函数。
//
// ## 这里没有装饰器，也没有进程级注册表
//
// 拆分前这一层是「`@Crontab` 装饰器往 WeakMap 里塞元数据 → `registerCrontabService`
// 在 import 期把实例推进一个全局数组 → `index.ts` 一句 `import './services'` 触发副作用」。
// 三个后果：谁被注册了只有运行时才知道；实例类型是 `any`；测试之间要互相 clear。这与
// 本服务已经否决过两次的东西是同一个（见 lark/ingress/lark-event.ts 的事件处理表、
// lark/rules/inbound-rules.ts 的规则序列），职责没变但形状必须变 —— 清单是**一份普通
// 数据**，任务本体由装配根递进来，进程里没有共享可变状态。
//
// ## 清单是账本，任务本体不在清单里
//
// 清单只记事实：任务名、cron 表达式、以谁的身份跑、还欠谁一条。这三样从 channel-server
// 那份还活着的实现里逐条对得上（schedule.test.ts 直接读对面的源码比），而任务本体要
// 飞书客户端、图库、emoji 仓储这些只有装配根才拿得到的东西，所以它从 `runs` 那一侧进来，
// 按任务名对上。**对不上就抛** —— key 拼错的症状本来是任务静默不挂，到点没发日报才有
// 人发现。
//
// ## lane gate 在函数里面，不在调用点
//
// channel-server 把这道判断写在装配处（`startup/application.ts:90`）。理由照抄它的：
// 定时任务的副作用是全局的 —— 日报往写死的真实飞书群发消息、emoji 每小时全量覆写共享
// 的 lark_emoji 表 —— 没有按泳道隔离的口径，泳道部署跑起来就是重复发群消息 + 写脏 prod
// 数据。位置换到函数里面是因为调用点在进程入口，那里的任何判断都只能靠读源码断言；
// 挪进来之后"prod 起、泳道不起"就是一条能真跑的行为测试。
//
// 判定复用 @inner/shared/lane-policy 的 isProdDeployment（它自己的文件头写着为什么这类
// 准入条件不许再挂一个环境变量），不重写。
//
// ## 它归入口进程，不归 lark-outbound
//
// 决策十那张进程表没写 cron 归谁。答案是入口进程：出站是竞争消费、可以多副本，每个副本
// 各起一份 cron 就是往那个写死的飞书群发 N 遍日报。入口进程是单副本的那个。

import { schedule as nodeCron } from 'node-cron';
import { context } from '@inner/shared/middleware';
import { isProdDeployment } from '@inner/shared/lane-policy';

/** 哪一批迁移负责把这个槽位填上。填好之后连同 `pendingIn` 一起删。 */
export type LarkScheduleBatch = 'D2' | 'D3';

/** 清单里的一格。全是事实，没有任务本体。 */
export interface LarkScheduleSlot {
    /** 任务名。日志里的标识，也是 `runs` 对上来的 key 和跨服务对账的键。 */
    readonly name: string;
    /** cron 表达式，五段：分 时 日 月 周。 */
    readonly cron: string;
    /** 以哪个 bot 的身份跑。它决定这次执行拿到哪套飞书凭据。 */
    readonly botName: string;
    /** 还欠着：记着谁负责填。填上本体的同时必须删掉这一项。 */
    readonly pendingIn?: LarkScheduleBatch;
}

/**
 * 飞书专属定时任务，三个。名字 / 表达式 / botName 与 channel-server 那份逐条相同。
 *
 * 今天三个槽位都还欠着，所以 prod 部署也一个任务都不挂 —— 与拆分前一致，因为它们
 * 此刻仍然由 channel-server 在跑。填一个槽位是两步：删掉这里的 `pendingIn`，往装配
 * 根的 `runs` 里加一个同名的本体。
 */
export const LARK_SCHEDULES: readonly LarkScheduleSlot[] = [
    // 每天 18:00：随机取一张已上传的图，发到订阅群，再往另一个群补一条带卡片的回复。
    { name: 'daily-photo', cron: '0 18 * * *', botName: 'tool', pendingIn: 'D2' },
    // 每天 19:30：昨天入库的新图汇成一张卡片发给特定群。
    { name: 'daily-new-photo', cron: '30 19 * * *', botName: 'tool', pendingIn: 'D2' },
    // 每小时：拉远端 emoji 表，原子替换本地 lark_emoji（复读功能唯一的读端）。
    { name: 'emoji-sync', cron: '0 * * * *', botName: 'chiwei', pendingIn: 'D3' },
];

/** 挂上去的一个 cron 任务。基座只需要它停得掉。 */
export interface CronJob {
    stop(): void | Promise<void>;
}

/**
 * 调度器端口。真身是 node-cron（见 `nodeCronScheduler`），测试传替身自己触发 ——
 * 定时任务的测试不许真的等时间流逝。
 */
export type CronScheduler = (cron: string, run: () => void) => CronJob;

/** node-cron 真身。`schedule` 返回的任务是自启动的。 */
export const nodeCronScheduler: CronScheduler = (cron, run) => nodeCron(cron, run);

export interface LarkScheduleWiring {
    /** 任务本体，key 是清单里的任务名。装配根把单例包进闭包后递进来。 */
    readonly runs: Readonly<Record<string, () => Promise<void>>>;
    readonly schedule: CronScheduler;
    /** 默认整份清单。测试用它换一份小的。 */
    readonly roster?: readonly LarkScheduleSlot[];
}

export interface LarkSchedules {
    /** 实际挂上去的任务名，按清单顺序。lane gate 挡住时是空数组。 */
    readonly running: readonly string[];
    stop(): void;
}

/**
 * 把清单挂到调度器上。返回的句柄给关停用 —— 停机过程中让任务再触发一次，就是拿一个
 * 正在关的 DB 连接去写库。
 */
export function startLarkSchedules(wiring: LarkScheduleWiring): LarkSchedules {
    const roster = wiring.roster ?? LARK_SCHEDULES;
    // 账本校验排在 lane gate 前面：泳道本来就不挂任务，接错了在那边永远看不见，
    // 等切到 prod 才第一次暴露 —— 那时它已经是线上问题了。
    reconcile(roster, wiring.runs);

    if (!isProdDeployment()) {
        console.info(
            `[lark-service] schedules skipped on lane=${process.env.LANE} — ` +
                'their side effects (a hard-coded Feishu group, a shared table) are prod-only',
        );
        return { running: [], stop: () => {} };
    }

    const jobs: CronJob[] = [];
    const running: string[] = [];
    for (const slot of roster) {
        const run = wiring.runs[slot.name];
        if (!run) continue;
        jobs.push(wiring.schedule(slot.cron, () => void fire(slot, run)));
        running.push(slot.name);
        console.info(`[lark-service] scheduled ${slot.name} at "${slot.cron}" as ${slot.botName}`);
    }
    console.info(`[lark-service] ${running.length} schedule(s) running`);

    return {
        running,
        stop: () => {
            for (const job of jobs) void job.stop();
        },
    };
}

/**
 * 清单与本体必须一一对上：`run` 存在 ⇔ `pendingIn` 不存在。两个方向都会静默失效
 * ——多出来的 key 是任务名拼错了（本体永远不跑），少掉的是槽位被谁清了 `pendingIn`
 * 却没接上本体（账本说搬完了，实际没有）。
 */
function reconcile(
    roster: readonly LarkScheduleSlot[],
    runs: Readonly<Record<string, () => Promise<void>>>,
): void {
    const problems: string[] = [];
    for (const slot of roster) {
        const hasRun = Boolean(runs[slot.name]);
        if (hasRun && slot.pendingIn) {
            problems.push(`${slot.name} has a run but is still marked pending in ${slot.pendingIn}`);
        }
        if (!hasRun && !slot.pendingIn) {
            problems.push(`${slot.name} has no run and no pendingIn`);
        }
    }
    const names = new Set(roster.map((slot) => slot.name));
    for (const name of Object.keys(runs)) {
        if (!names.has(name)) problems.push(`${name} is not on the roster`);
    }
    if (problems.length > 0) {
        throw new Error(`lark-service: schedule roster and runs disagree — ${problems.join('; ')}`);
    }
}

/**
 * 跑一次。**错误在这里就地吞掉**：进程入口的 unhandledRejection 处理器是
 * `process.exit(1)`，一次 emoji 同步失败会把持着飞书长连的那个进程带走。
 *
 * 每次执行现开一个上下文：botName 决定这次拿哪套飞书凭据，traceId 让同一次执行的
 * 日志和下游调用串得起来（所以是每次一个，不是挂上去时一个）。
 */
async function fire(slot: LarkScheduleSlot, run: () => Promise<void>): Promise<void> {
    console.info(`[lark-service] ${slot.name} started`);
    try {
        await context.run(context.createContext(undefined, { botName: slot.botName }), run);
        console.info(`[lark-service] ${slot.name} finished`);
    } catch (error) {
        console.error(`[lark-service] ${slot.name} failed:`, error);
    }
}
