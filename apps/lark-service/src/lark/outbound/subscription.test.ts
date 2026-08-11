// 出站订阅的生命周期：这一刻该不该消费、结论变了之后怎么原地生效。
//
// 这一层不认识飞书，也不认识队列里装的是什么。chat_response 和 recall 两条队列共用
// 它，**而且共用同一把开关** —— 这不是省事，是对称性要求：channel-server 那一侧的
// 两个 worker 各构造一个 OutboundSubscriptions，但 loadChannels 读的是同一把
// Dynamic Config key，运维把 lark 从清单里摘掉时两条队列是同时被释放的。接管侧用
// 两把开关就会出现「只翻了一把，另一条队列没有任何消费者」的窗口。
//
// 所以这个文件里最重要的一条断言是「一把开关，两条队列同进同退」。

import { describe, expect, it } from 'bun:test';
import type { ConsumeMessage } from 'amqplib';
import type { Route } from '@inner/shared/mq';

import {
    LarkOutboundSubscriptions,
    parseConsumeSwitch,
    type OutboundQueueBinding,
    type OutboundSubscriptionPort,
} from './subscription';

const ALPHA: Route = { queue: 'alpha', rk: 'alpha.rk' };
const BETA: Route = { queue: 'beta', rk: 'beta.rk' };

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

/**
 * 替身必须还原真实端口的**副作用顺序**，不然测出来的自愈是假的。
 *
 * `RabbitMQClient.consume` 是先把订阅项 push 进重连恢复列表、再去 broker 注册的，
 * 所以注册抛错时那一项已经躺在恢复列表里且可恢复 —— 断线重连会把它订回来。
 * `RabbitMQClient.drainConsumer` 是先摘掉重连资格、先发 basic.cancel、再等在途归零
 * 的，所以超时抛错时 broker 侧**已经取消了**。
 *
 * 「什么都没做就抛」的替身会错误地证明「下一次一定自愈」：它让"失败=什么都没发生"，
 * 而真实端口两个入口都是「先产生副作用、再可能失败」。
 */
interface Amqp {
    port: OutboundSubscriptionPort;
    declared: Route[];
    /** broker 眼里"这条队列上挂着一个会被投递的消费者"。 */
    consuming: Map<string, (msg: ConsumeMessage) => Promise<void>>;
    /** 每一次 consume 都记一笔（consuming 是 Map，重复订阅会被它盖掉看不出来）。 */
    subscribeLog: string[];
    drained: string[];
    /** 带顺序的全量流水：跨 consume / drain 断言先后时用它。 */
    ops: string[];
    /** 订阅这条队列时抛错 —— **先登记再抛**，见上面的注释。 */
    failConsumeOn?: string;
    /** drain 这条队列时抛错（在途没归零）—— **先取消再抛**。 */
    failDrainOn?: string;
    /** 注入慢订阅：用来观察 reconcile 重入。 */
    beforeConsume?: () => Promise<void>;
    /** 端口断线重连：恢复列表里的订阅项被重新注册（真实端口 5 秒后就会做）。 */
    reconnect(): void;
}

function fakeAmqp(): Amqp {
    const declared: Route[] = [];
    const consuming = new Map<string, (msg: ConsumeMessage) => Promise<void>>();
    const recovering = new Map<string, (msg: ConsumeMessage) => Promise<void>>();
    const subscribeLog: string[] = [];
    const drained: string[] = [];
    const ops: string[] = [];

    const amqp: Amqp = {
        declared,
        consuming,
        subscribeLog,
        drained,
        ops,
        reconnect: () => {
            for (const [queue, handler] of recovering) consuming.set(queue, handler);
        },
        port: {
            declareRoute: async (route) => void declared.push(route),
            consume: async (queue, handler) => {
                await amqp.beforeConsume?.();
                subscribeLog.push(queue);
                ops.push(`consume ${queue}`);
                // 先进恢复列表、再去 broker 注册：注册抛错时那一项已经在列表里且
                // 可恢复，broker 侧却还没有消费者。
                recovering.set(queue, handler);
                if (amqp.failConsumeOn === queue) {
                    throw new Error(`broker refused ${queue}`);
                }
                consuming.set(queue, handler);
            },
            drainConsumer: async (queue) => {
                // 先摘重连资格、先发 basic.cancel，再等在途归零：超时抛错时前两件
                // 已经做完了。
                drained.push(queue);
                ops.push(`drain ${queue}`);
                recovering.delete(queue);
                consuming.delete(queue);
                if (amqp.failDrainOn === queue) {
                    throw new Error(`[RabbitMQ] drain timed out on ${queue}`);
                }
            },
        },
    };
    return amqp;
}

interface Recorder {
    binding: OutboundQueueBinding;
    /** 收到的消息，连同「handler 是被哪条队列名造出来的」。 */
    got: Array<{ queue: string; body: string }>;
}

function recorder(route: Route): Recorder {
    const got: Array<{ queue: string; body: string }> = [];
    return {
        got,
        binding: {
            route,
            handler: (queue) => async (msg) => {
                got.push({ queue, body: msg.content.toString() });
            },
        },
    };
}

function message(body: string): ConsumeMessage {
    return { content: Buffer.from(body) } as unknown as ConsumeMessage;
}

interface Harness {
    amqp: Amqp;
    alpha: Recorder;
    beta: Recorder;
    subscriptions: LarkOutboundSubscriptions;
    setSwitch(raw: string): void;
    breakSwitch(error: Error | null): void;
    reconcile(): Promise<void>;
    queues(): string[];
}

function start(options: { switchValue?: string; lane?: string } = {}): Harness {
    const amqp = fakeAmqp();
    const alpha = recorder(ALPHA);
    const beta = recorder(BETA);
    let raw = options.switchValue ?? 'true';
    let broken: Error | null = null;

    const subscriptions = new LarkOutboundSubscriptions({
        amqp: amqp.port,
        lane: options.lane,
        queues: [alpha.binding, beta.binding],
        readConsumeSwitch: async () => {
            if (broken) throw broken;
            return raw;
        },
    });

    return {
        amqp,
        alpha,
        beta,
        subscriptions,
        setSwitch: (value) => {
            raw = value;
        },
        breakSwitch: (error) => {
            broken = error;
        },
        reconcile: () => subscriptions.reconcile(),
        queues: () => subscriptions.subscribedQueues(),
    };
}

// ---------------------------------------------------------------------------
// 一把开关，两条队列
// ---------------------------------------------------------------------------

describe('一把开关同时管住两条队列', () => {
    it('翻开：两条队列各自声明、各自订上', async () => {
        const h = start({ switchValue: 'false' });
        expect(h.queues()).toEqual([]);

        h.setSwitch('true');
        await h.reconcile();

        expect(h.queues()).toEqual(['alpha', 'beta']);
        expect(h.amqp.declared).toEqual([ALPHA, BETA]);
    });

    it('翻回去：两条队列都走 drain 屏障交还，不是只交一条', async () => {
        // 只交一条的后果是另一条队列既没被交还、又不再被 reconcile 关照 —— 交接完成
        // 之后它仍然挂着一个消费者，跟接手方分摊消息。
        const h = start({ switchValue: 'true' });
        await h.reconcile();

        h.setSwitch('false');
        await h.reconcile();

        expect(h.amqp.drained).toEqual(['alpha', 'beta']);
        expect(h.queues()).toEqual([]);
    });

    it('关着的时候一条都不订、一次声明都不发', async () => {
        const h = start({ switchValue: 'false' });
        await h.reconcile();

        expect(h.queues()).toEqual([]);
        expect(h.amqp.declared).toEqual([]);
        expect(h.amqp.subscribeLog).toEqual([]);
    });

    it('泳道部署订的是两条带泳道后缀的队列', async () => {
        const h = start({ lane: 'ppe-x' });
        await h.reconcile();

        expect(h.queues()).toEqual(['alpha_ppe-x', 'beta_ppe-x']);
    });

    it('每条队列的消息只交给自己那个 handler', async () => {
        const h = start();
        await h.reconcile();

        await h.amqp.consuming.get('alpha')!(message('to-alpha'));
        await h.amqp.consuming.get('beta')!(message('to-beta'));

        expect(h.alpha.got).toEqual([{ queue: 'alpha', body: 'to-alpha' }]);
        expect(h.beta.got).toEqual([{ queue: 'beta', body: 'to-beta' }]);
    });
});

// ---------------------------------------------------------------------------
// 读不到有效指令
// ---------------------------------------------------------------------------

describe('没拿到有效指令就保持上次的结论', () => {
    it('启动时读失败（抛异常）：按关处理，绝不自己变宽', async () => {
        const h = start();
        h.breakSwitch(new Error('paas-engine unreachable'));

        await h.reconcile();

        expect(h.queues()).toEqual([]);
    });

    it('空串 / 看不懂的值：同样按"没有有效指令"处理', async () => {
        for (const value of ['', '待定']) {
            const h = start({ switchValue: value });
            await h.reconcile();
            expect(h.queues()).toEqual([]);
        }
    });

    it('已经在消费之后读失败：两条队列都保持消费，一条都不扔', async () => {
        // 这个方向的"变宽"是停止消费：一次 paas-engine 抖动就让飞书的回复和撤回同时
        // 没人处理，泳道队列 10 秒后弹回 prod。
        const h = start({ switchValue: 'true' });
        await h.reconcile();

        h.breakSwitch(new Error('paas-engine unreachable'));
        await h.reconcile();

        expect(h.queues()).toEqual(['alpha', 'beta']);
        expect(h.amqp.drained).toEqual([]);
    });

    it('parseConsumeSwitch 把三种读数分开：开 / 关 / 没有有效指令', () => {
        for (const on of ['true', '1', 'yes', 'TRUE', ' Yes ']) {
            expect(parseConsumeSwitch(on)).toBe(true);
        }
        for (const off of ['false', '0', 'no', 'FALSE']) {
            expect(parseConsumeSwitch(off)).toBe(false);
        }
        for (const unknown of ['', '   ', '待定', 'null']) {
            expect(parseConsumeSwitch(unknown)).toBeNull();
        }
    });
});

// ---------------------------------------------------------------------------
// 重复与并发
// ---------------------------------------------------------------------------

describe('重复 reconcile 不重复订阅', () => {
    it('一直开着：反复 reconcile 也只订一次', async () => {
        // 同一个进程订两次等于自己跟自己分摊消息，prefetch 也翻倍。
        const h = start({ switchValue: 'true' });

        await h.reconcile();
        await h.reconcile();
        await h.reconcile();

        expect(h.amqp.subscribeLog).toEqual(['alpha', 'beta']);
        expect(h.amqp.declared).toHaveLength(2);
    });

    it('两次 reconcile 撞在一起：只订一遍', async () => {
        // 定时器的间隔比 drain 的最坏耗时短，两次 reconcile 重叠是常态而非意外。
        const h = start({ switchValue: 'false' });

        let release!: () => void;
        const slow = new Promise<void>((resolve) => {
            release = resolve;
        });
        h.amqp.beforeConsume = () => slow;
        h.setSwitch('true');

        const first = h.reconcile();
        const second = h.reconcile();
        release();
        await Promise.all([first, second]);

        expect(h.amqp.subscribeLog).toEqual(['alpha', 'beta']);
    });

    it('半路失败：下一次 reconcile 只补没订上的那条，不把已订的再订一遍', async () => {
        // 记账必须按队列逐条记。整体记一个"已经开了"的话，第二条队列永远补不上 ——
        // 而它的表现是"撤回默默没人消费"，队列在涨、服务全部健康。
        const h = start({ switchValue: 'true' });
        h.amqp.failConsumeOn = 'beta';

        await expect(h.reconcile()).rejects.toThrow('broker refused beta');
        expect(h.queues()).toEqual(['alpha']);

        h.amqp.failConsumeOn = undefined;
        await h.reconcile();

        // alpha 只订一次；beta 补上了。beta 那两笔之间夹着一次 drain，理由见下一个用例。
        expect(h.amqp.subscribeLog.filter((q) => q === 'alpha')).toEqual(['alpha']);
        expect(h.queues()).toEqual(['alpha', 'beta']);
        expect([...h.amqp.consuming.keys()].sort()).toEqual(['alpha', 'beta']);
    });

    it('consume 抛错、端口重连把它订回来：重订之前先摘干净，broker 上不留两个消费者', async () => {
        // 真实端口 consume 抛错时订阅项已经在重连恢复列表里，5 秒后的重连会把它订
        // 回来并写上新的 consumerTag。此时直接再 consume 一次 = broker 上两个消费者，
        // 而旧那个的 tag 已经被覆盖、再也 cancel 不掉 —— 它会活过下一次移交，跟接手
        // 方分摊同一条队列。
        const h = start({ switchValue: 'true' });
        h.amqp.failConsumeOn = 'beta';
        await expect(h.reconcile()).rejects.toThrow('broker refused beta');
        // 注册没成功：broker 侧还没有 beta 的消费者，但恢复列表里有。
        expect([...h.amqp.consuming.keys()]).toEqual(['alpha']);

        h.amqp.reconnect();
        expect([...h.amqp.consuming.keys()].sort()).toEqual(['alpha', 'beta']);

        h.amqp.failConsumeOn = undefined;
        await h.reconcile();

        expect(h.amqp.ops).toEqual(['consume alpha', 'consume beta', 'drain beta', 'consume beta']);
    });
});

// ---------------------------------------------------------------------------
// 操作失败之后的簿记
// ---------------------------------------------------------------------------

describe('操作抛错时按「副作用已经发生」记账', () => {
    it('consume 抛错之后开关翻到关：那条队列照样走 drain 交还', async () => {
        // 抛错不代表没订上：订阅项已经躺在端口的重连恢复列表里，断线重连会把它订
        // 回来。记成"没订上"的话，开关翻回去时 reconcile 看不出 diff —— 重连之后
        // 冒出来的那个消费者永远不会被 cancel，交接完成之后还在分摊消息。
        const h = start({ switchValue: 'true' });
        h.amqp.failConsumeOn = 'beta';
        await expect(h.reconcile()).rejects.toThrow('broker refused beta');

        h.setSwitch('false');
        await h.reconcile();

        expect(h.amqp.drained).toEqual(['alpha', 'beta']);
        expect(h.queues()).toEqual([]);
    });

    it('drain 抛错之后开关翻回开：那条队列被重新订上，不是永远没人消费', async () => {
        // drain 超时抛错时 basic.cancel 早就发出去了，broker 侧已经不投递。记成
        // "还订着"的话，开关翻回来时 reconcile 同样看不出 diff —— 这条队列从此没有
        // 任何消费者，没有错误也没有告警，消息就在里面堆着。
        const h = start({ switchValue: 'true' });
        await h.reconcile();

        h.amqp.failDrainOn = 'beta';
        h.setSwitch('false');
        await expect(h.reconcile()).rejects.toThrow('drain timed out on beta');

        h.amqp.failDrainOn = undefined;
        h.setSwitch('true');
        await h.reconcile();

        expect([...h.amqp.consuming.keys()].sort()).toEqual(['alpha', 'beta']);
        expect(h.queues().sort()).toEqual(['alpha', 'beta']);
    });

    it('一条都没订上之后开关读不到：按上次读到的「开」重试，不是当成关', async () => {
        // 「读不到就保持上次结论」保的是**上次开关的结论**，不是"当前订着几条"。
        // 两者混成一个状态时，首次翻开就订阅失败会被读成"上次是关的" —— 开关明明
        // 是开的，却再也不会重试。
        const h = start({ switchValue: 'true' });
        h.amqp.failConsumeOn = 'alpha';
        await expect(h.reconcile()).rejects.toThrow('broker refused alpha');
        expect(h.queues()).toEqual([]);

        h.amqp.failConsumeOn = undefined;
        h.breakSwitch(new Error('paas-engine unreachable'));
        await h.reconcile();

        expect(h.queues()).toEqual(['alpha', 'beta']);
    });
});
