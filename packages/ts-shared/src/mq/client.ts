import amqplib, { Channel, ChannelModel, ConfirmChannel, ConsumeMessage } from 'amqplib';
// traceId 属于 base context（@middleware/context 的 context 就是 spread 了它、共用同
// 一个 AsyncLocalStorage），这里只需要 traceId，取基座这层即可。
import { context } from '../middleware/context';

const EXCHANGE_NAME = 'post_processing';
const DLX_NAME = 'post_processing_dlx';
const DLQ_NAME = 'dead_letters';

// Route 当前假设 queue:rk = 1:1，未来如果需要演进为更灵活的结构需另行讨论。
export interface Route {
    readonly queue: string;
    readonly rk: string;
}

export const CHAT_REQUEST: Route = { queue: 'chat_request', rk: 'chat.request' };
export const CHAT_RESPONSE: Route = { queue: 'chat_response', rk: 'chat.response' };
export const RECALL: Route = { queue: 'recall', rk: 'action.recall' };
export const PROACTIVE_EVAL: Route = { queue: 'proactive_eval', rk: 'proactive.eval' };

const ALL_ROUTES: Route[] = [CHAT_REQUEST, CHAT_RESPONSE, RECALL, PROACTIVE_EVAL];

const NON_PROD_EXPIRES_MS = 86_400_000;
const LANE_FALLBACK_TTL_MS = 10_000;

export type MessageHandler = (msg: ConsumeMessage) => Promise<void>;

/** 获取当前泳道：读环境变量，prod/空返回 undefined */
export function getLane(): string | undefined {
    const lane = process.env.LANE;
    if (!lane || lane === 'prod') return undefined;
    return lane;
}

/** 泳道队列名：base 或 base_{lane} */
export function laneQueue(base: string, lane?: string): string {
    return lane ? `${base}_${lane}` : base;
}

/** 泳道 routing key：base 或 base.{lane} */
export function laneRK(base: string, lane?: string): string {
    return lane ? `${base}.${lane}` : base;
}

function buildQueueArgs(prodRK: string, lane?: string): Record<string, unknown> {
    const extra: Record<string, unknown> = lane ? { 'x-expires': NON_PROD_EXPIRES_MS } : {};
    if (!lane) {
        return { 'x-dead-letter-exchange': DLX_NAME, ...extra };
    }
    return {
        'x-message-ttl': LANE_FALLBACK_TTL_MS,
        'x-dead-letter-exchange': EXCHANGE_NAME,
        'x-dead-letter-routing-key': prodRK,
        ...extra,
    };
}

class RabbitMQClient {
    private static instance: RabbitMQClient;
    private conn: ChannelModel | null = null;
    private channel: Channel | null = null;
    /** 建到一半的 confirm channel 也算数，理由见 getConfirmChannel。 */
    private confirmChannel: Promise<ConfirmChannel> | null = null;
    private reconnecting = false;
    private consumers: Array<{ queue: string; handler: MessageHandler }> = [];
    private declaredLaneQueues = new Set<string>();

    private constructor() {}

    static getInstance(): RabbitMQClient {
        if (!RabbitMQClient.instance) {
            RabbitMQClient.instance = new RabbitMQClient();
        }
        return RabbitMQClient.instance;
    }

    async connect(): Promise<void> {
        if (this.channel) return;

        const url = process.env.RABBITMQ_URL;
        if (!url) {
            throw new Error('RABBITMQ_URL is not configured');
        }

        this.conn = await amqplib.connect(url);

        this.conn.on('error', (err: Error) => {
            console.error('[RabbitMQ] connection error:', err.message);
        });
        this.conn.on('close', () => {
            console.warn('[RabbitMQ] connection closed, will reconnect');
            this.channel = null;
            // 连接没了，挂在它上面的 confirm channel 也没了。留着旧引用的话，下一次
            // 发送会在一个已经死掉的 channel 上等一个永远不来的确认。
            this.confirmChannel = null;
            this.conn = null;
            this.scheduleReconnect();
        });

        this.channel = await this.conn.createChannel();
        await this.channel.prefetch(10);
        console.info('[RabbitMQ] connected');
    }

    async declareTopology(): Promise<void> {
        const ch = this.getChannel();
        const lane = getLane();

        // DLX + DLQ
        await ch.assertExchange(DLX_NAME, 'fanout', { durable: true });
        await ch.assertQueue(DLQ_NAME, { durable: true });
        await ch.bindQueue(DLQ_NAME, DLX_NAME, '');

        // Main exchange (delayed-message)
        await ch.assertExchange(EXCHANGE_NAME, 'x-delayed-message', {
            durable: true,
            arguments: { 'x-delayed-type': 'topic' },
        });

        for (const route of ALL_ROUTES) {
            const qName = laneQueue(route.queue, lane);
            await ch.assertQueue(qName, {
                durable: true,
                arguments: buildQueueArgs(route.rk, lane),
            });
            await ch.bindQueue(qName, EXCHANGE_NAME, laneRK(route.rk, lane));
        }

        console.info(`[RabbitMQ] topology declared (lane=${lane || 'prod'})`);
    }

    private async ensureLaneQueue(route: Route, lane: string): Promise<void> {
        const cacheKey = `${route.queue}_${lane}`;
        if (this.declaredLaneQueues.has(cacheKey)) return;
        const ch = this.getChannel();
        const qName = laneQueue(route.queue, lane);
        await ch.assertQueue(qName, { durable: true, arguments: buildQueueArgs(route.rk, lane) });
        await ch.bindQueue(qName, EXCHANGE_NAME, laneRK(route.rk, lane));
        this.declaredLaneQueues.add(cacheKey);
        console.info(`[RabbitMQ] Lazy-declared lane queue: ${route.queue}_${lane}`);
    }

    async publish(
        route: Route,
        body: Record<string, unknown>,
        delayMs?: number,
        headers?: Record<string, unknown>,
        lane?: string,
    ): Promise<void> {
        const ch = this.getChannel();

        // 默认取当前泳道；传入 lane 则使用传入值
        const effectiveLane = lane !== undefined ? (lane === 'prod' ? undefined : lane) : getLane();

        if (effectiveLane) {
            await this.ensureLaneQueue(route, effectiveLane);
        }

        const actualRK = laneRK(route.rk, effectiveLane);

        // 泳道和 trace 必须落在 AMQP header 上：下游 agent-service 的
        // runtime/propagation.py::extract_context 只认 header 的 "lane" / "trace_id"，
        // 队列名和消息体里的泳道它读不到。空值写空串而不是省略 key，与 Python 侧
        // inject_context 的线上格式一致。
        //
        // 调用方 header 先展开、lane / trace_id 后写：这两个是 publish 内部定的权威
        // 值，调用方覆盖不了。lane 尤其不能让：routing key 用的就是上面这个
        // effectiveLane，header 被改写就会出现「消息进 queue_Y、header 却写着 X」，
        // 下游按 header 判 lane 直接判错。调用方自己的 header（x-retry-count 之类）
        // 不在这两个 key 上，照常保留。
        const msgHeaders: Record<string, unknown> = {
            ...headers,
            trace_id: context.getTraceId() || '',
            lane: effectiveLane || '',
        };
        if (delayMs !== undefined) {
            msgHeaders['x-delay'] = delayMs;
        }

        ch.publish(EXCHANGE_NAME, actualRK, Buffer.from(JSON.stringify(body)), {
            persistent: true,
            contentType: 'application/json',
            headers: msgHeaders,
        });
    }

    async consume(queueName: string, handler: MessageHandler): Promise<void> {
        // 记录 consumer 以便重连后恢复
        if (!this.consumers.some((c) => c.queue === queueName)) {
            this.consumers.push({ queue: queueName, handler });
        }
        await this.registerConsumer(queueName, handler);
    }

    private async registerConsumer(queueName: string, handler: MessageHandler): Promise<void> {
        const ch = this.getChannel();
        await ch.consume(queueName, async (msg) => {
            if (!msg) return;
            try {
                await handler(msg);
            } catch (err) {
                console.error(`[RabbitMQ] handler error on ${queueName}:`, err);
                ch.nack(msg, false, false);
            }
        });
        console.info(`[RabbitMQ] consuming queue: ${queueName}`);
    }

    ack(msg: ConsumeMessage): void {
        try {
            this.getChannel().ack(msg);
        } catch (e) {
            console.warn('[RabbitMQ] ack failed (channel likely closed):', (e as Error).message);
        }
    }

    nack(msg: ConsumeMessage, requeue = false): void {
        try {
            this.getChannel().nack(msg, false, requeue);
        } catch (e) {
            console.warn('[RabbitMQ] nack failed (channel likely closed):', (e as Error).message);
        }
    }

    getChannel(): Channel {
        if (!this.channel) {
            throw new Error('RabbitMQ channel not available; call connect() first');
        }
        return this.channel;
    }

    /**
     * 带发送确认的 channel。
     *
     * 普通 channel 的 publish / sendToQueue 是「写进本地缓冲就返回 true」——
     * `persistent: true` 只约束 broker 收到之后要落盘，**不证明 broker 收到了**。
     * 连接恰好在这时断掉，消息就静默没了。发出去之后本地不留账、丢了没法补的场景
     * （跨泳道交接就是），必须用这个并等确认。
     *
     * 懒建：只有真的要发的进程才多一条 channel。channel 是连接上的多路复用流，
     * 不是新连接，所以增量可以忽略。
     *
     * 缓存的是**建到一半的那个 promise**，不是建好的 channel。缓存写在 await 之后
     * 的话，并发的首次调用会全部看到空、各建一条，最后只有一条被记住，其余的挂在
     * 连接上直到断开（AMQP 的 channel 数有上限）。飞书交接正是这种形状：进程刚起来
     * 时多条消息同时第一次调到这里。
     *
     * 缓存的失效有两条路：连接的 close（上面那个 handler）和 channel 自己的
     * close / error。后者不能省 —— AMQP 里 broker 可以在连接仍然活着的时候单独关掉
     * 一条 channel（协议错误、队列冲突等），死 channel 留在缓存里的话，之后每一次
     * 交接投递都会在它上面等一个永远不来的确认。
     */
    getConfirmChannel(): Promise<ConfirmChannel> {
        if (this.confirmChannel) return this.confirmChannel;
        const conn = this.conn;
        if (!conn) {
            return Promise.reject(
                new Error('RabbitMQ connection not available; call connect() first'),
            );
        }
        // 只清"缓存里还是我这条"的时候。一条 channel 出事时 amqplib 会先 error 再
        // close，事件还可能晚于我们重建的那条到 —— 无脑置空就把好端端的新 channel
        // 也丢了，每次交接都白建一条。
        const forget = (): void => {
            if (this.confirmChannel === creating) this.confirmChannel = null;
        };
        const creating: Promise<ConfirmChannel> = conn
            .createConfirmChannel()
            .then((channel) => {
                channel.on('close', forget);
                channel.on('error', forget);
                return channel;
            })
            // 建失败也要清掉，否则所有人永远拿到同一个 rejected promise，连接恢复
            // 了也起不来。
            .catch((error) => {
                forget();
                throw error;
            });
        this.confirmChannel = creating;
        return creating;
    }

    async close(): Promise<void> {
        try {
            // 正在建的那条也要等出来再关，不然它会在连接关掉之后才建好、没人管。
            await (await this.confirmChannel)?.close();
            await this.channel?.close();
            await this.conn?.close();
        } catch {
            // ignore close errors
        }
        this.confirmChannel = null;
        this.channel = null;
        this.conn = null;
    }

    private scheduleReconnect(): void {
        if (this.reconnecting) return;
        this.reconnecting = true;
        this.declaredLaneQueues.clear();
        setTimeout(async () => {
            this.reconnecting = false;
            try {
                await this.connect();
                await this.declareTopology();
                for (const { queue, handler } of this.consumers) {
                    await this.registerConsumer(queue, handler);
                }
                console.info('[RabbitMQ] reconnected');
            } catch (err) {
                console.error('[RabbitMQ] reconnect failed:', err);
                this.scheduleReconnect();
            }
        }, 5000);
    }
}

export const rabbitmqClient = RabbitMQClient.getInstance();

// 暴露共享 amqp Channel 给需要直接操作 Channel 的模块（inbound-lane 的 fail-closed
// 队列不能走 publish/consume 那套默认 lane 队列参数，必须自己 assertQueue）。
export function getRabbitChannel(): Channel {
    return rabbitmqClient.getChannel();
}

// 需要「broker 确认收到了」而不只是「写进缓冲了」的发送走这个。见 getConfirmChannel。
export function getRabbitConfirmChannel(): Promise<ConfirmChannel> {
    return rabbitmqClient.getConfirmChannel();
}
