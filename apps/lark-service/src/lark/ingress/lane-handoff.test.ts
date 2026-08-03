// 交接的投递侧。判断（该走哪条泳道）在 chooseInboundLane，投递（写进哪个队列）在
// handOffToInboundLane —— 两件事分开测，因为一个是纯决策、一个是 MQ 的具体形态。

import { describe, expect, it } from 'bun:test';

import { chooseInboundLane, handOffToInboundLane } from './lane-handoff';
import type { InboundLaneEnvelope } from './lane-queue';

function envelope(overrides: Partial<InboundLaneEnvelope> = {}): InboundLaneEnvelope {
    return {
        channel: 'lark',
        event_type: 'im.message.receive_v1',
        global_message_id: 'cm_1',
        trace_id: 'trace-1',
        lane: 'ppe-x',
        bot_name: 'chiwei',
        params: { message: { message_id: 'om_1' } },
        ...overrides,
    };
}

describe('chooseInboundLane', () => {
    it('开关关着时不算泳道，本地处理', async () => {
        let asked = 0;
        const choice = await chooseInboundLane({
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
    // 在绑定刚改过时把消息二次转投，甚至投进没人消费的 inbound_lane.prod。
    it('本进程不是 prod 时不再判，直接本地处理', async () => {
        let asked = 0;
        const choice = await chooseInboundLane({
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
            dispatchEnabled: true,
            currentLane: 'prod',
            laneOf: async () => 'prod',
        });

        expect(choice).toEqual({ handOff: false, lane: 'prod' });
    });

    it('算出来是别的泳道时交出去', async () => {
        const choice = await chooseInboundLane({
            dispatchEnabled: true,
            currentLane: 'prod',
            laneOf: async () => 'ppe-x',
        });

        expect(choice).toEqual({ handOff: true, lane: 'ppe-x' });
    });
});

describe('handOffToInboundLane', () => {
    /** 默认自动确认；传 'manual' 时由测试自己决定什么时候确认。 */
    function amqpDouble(mode: 'auto' | 'manual' = 'auto') {
        const declared: Array<{ queue: string; options: unknown }> = [];
        const sent: Array<{ queue: string; body: unknown; options: unknown }> = [];
        let confirm: ((error: Error | null) => void) | undefined;
        return {
            declared,
            sent,
            confirm: (error: Error | null = null) => confirm!(error),
            amqp: {
                assertQueue: async (queue: string, options: unknown) => {
                    declared.push({ queue, options });
                },
                sendToQueue: (
                    queue: string,
                    body: Buffer,
                    options: unknown,
                    confirmed: (error: Error | null) => void,
                ) => {
                    sent.push({ queue, body: JSON.parse(body.toString()), options });
                    confirm = confirmed;
                    if (mode === 'auto') confirmed(null);
                    return true;
                },
            },
        };
    }

    it('投进目标泳道的队列，信封原样序列化', async () => {
        const { amqp, sent } = amqpDouble();
        const env = envelope();

        await handOffToInboundLane(amqp, env);

        expect(sent).toHaveLength(1);
        expect(sent[0]!.queue).toBe('inbound_lane.ppe-x');
        expect(sent[0]!.body).toEqual(env as unknown as Record<string, unknown>);
    });

    // 装在里面的是「已经判定该在这条泳道处理」的消息。配 TTL 就是让它过期跑回
    // prod，等于拿泳道的改动去污染线上；配死信则会因为没有 prod 基队列直接丢。
    it('队列是 durable 的，且不配 TTL、不配死信', async () => {
        const { amqp, declared } = amqpDouble();

        await handOffToInboundLane(amqp, envelope());

        expect(declared).toEqual([{ queue: 'inbound_lane.ppe-x', options: { durable: true } }]);
    });

    it('消息本身是持久化的', async () => {
        const { amqp, sent } = amqpDouble();

        await handOffToInboundLane(amqp, envelope());

        expect(sent[0]!.options).toEqual({ persistent: true });
    });

    // codex 指出的破口：persistent 只约束「broker 收到之后要落盘」，**不证明
    // broker 收到了**。普通 channel 的 sendToQueue 写进本地缓冲就返回 true，连接在
    // 那一刻断掉的话消息静默没了 —— 而分叉那一支紧接着 return，本地没有任何账可以
    // 用来恢复。所以必须等 broker 的确认。
    it('broker 确认之前不算投递成功', async () => {
        const { amqp, confirm } = amqpDouble('manual');
        let done = false;

        const handedOff = handOffToInboundLane(amqp, envelope()).then(() => {
            done = true;
        });
        await Bun.sleep(1);
        expect(done).toBe(false);

        confirm(null);
        await handedOff;
        expect(done).toBe(true);
    });

    // 确认失败要走可重试路径（往上抛），不能当成功。
    it('broker 拒收或连接断掉时抛错', async () => {
        const { amqp, confirm } = amqpDouble('manual');

        const handedOff = handOffToInboundLane(amqp, envelope());
        // 队列声明先于发送，所以要等它落地才拿得到确认回调
        await Bun.sleep(1);
        confirm(new Error('channel closed'));

        await expect(handedOff).rejects.toThrow(/inbound_lane\.ppe-x.*channel closed/);
    });

    // 声明失败就直接抛，绝不「先发了再说」—— 队列参数不对时消息会落进一个语义
    // 不对的队列里。
    it('队列声明失败时不发消息', async () => {
        const { sent } = amqpDouble();
        const amqp = {
            assertQueue: async () => {
                throw new Error('channel closed');
            },
            sendToQueue: () => {
                sent.push({ queue: '', body: null, options: null });
                return true;
            },
        };

        await expect(handOffToInboundLane(amqp, envelope())).rejects.toThrow('channel closed');
        expect(sent).toEqual([]);
    });
});
