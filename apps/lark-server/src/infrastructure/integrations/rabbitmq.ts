import amqplib, { Channel, ChannelModel, ConsumeMessage } from 'amqplib';

const EXCHANGE_NAME = 'post_processing';
const DLX_NAME = 'post_processing_dlx';
const DLQ_NAME = 'dead_letters';

export const QUEUE_RECALL = 'recall';
export const QUEUE_VECTORIZE = 'vectorize';
export const QUEUE_CHAT_REQUEST = 'chat_request';
export const QUEUE_CHAT_RESPONSE = 'chat_response';
export const RK_RECALL = 'action.recall';
export const RK_VECTORIZE = 'task.vectorize';
export const RK_CHAT_REQUEST = 'chat.request';
export const RK_CHAT_RESPONSE = 'chat.response';

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

class RabbitMQClient {
    private static instance: RabbitMQClient;
    private conn: ChannelModel | null = null;
    private channel: Channel | null = null;
    private reconnecting = false;
    private consumers: Array<{ queue: string; handler: MessageHandler }> = [];

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

        // 队列参数：prod 队列用 DLX → DLQ；lane 队列用 TTL → 主 exchange fallback 到 prod
        function queueArgs(prodRK: string): Record<string, unknown> {
            const extra: Record<string, unknown> = lane
                ? { 'x-expires': NON_PROD_EXPIRES_MS }
                : {};
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

        // recall queue
        const recallQ = laneQueue(QUEUE_RECALL, lane);
        await ch.assertQueue(recallQ, { durable: true, arguments: queueArgs(RK_RECALL) });
        await ch.bindQueue(recallQ, EXCHANGE_NAME, laneRK(RK_RECALL, lane));

        // vectorize queue
        const vectorizeQ = laneQueue(QUEUE_VECTORIZE, lane);
        await ch.assertQueue(vectorizeQ, { durable: true, arguments: queueArgs(RK_VECTORIZE) });
        await ch.bindQueue(vectorizeQ, EXCHANGE_NAME, laneRK(RK_VECTORIZE, lane));

        // chat_request queue
        const chatReqQ = laneQueue(QUEUE_CHAT_REQUEST, lane);
        await ch.assertQueue(chatReqQ, { durable: true, arguments: queueArgs(RK_CHAT_REQUEST) });
        await ch.bindQueue(chatReqQ, EXCHANGE_NAME, laneRK(RK_CHAT_REQUEST, lane));

        // chat_response queue
        const chatRespQ = laneQueue(QUEUE_CHAT_RESPONSE, lane);
        await ch.assertQueue(chatRespQ, { durable: true, arguments: queueArgs(RK_CHAT_RESPONSE) });
        await ch.bindQueue(chatRespQ, EXCHANGE_NAME, laneRK(RK_CHAT_RESPONSE, lane));

        console.info(`[RabbitMQ] topology declared (lane=${lane || 'prod'})`);
    }

    async publish(
        routingKey: string,
        body: Record<string, unknown>,
        delayMs?: number,
        headers?: Record<string, unknown>,
        lane?: string,
    ): Promise<void> {
        const ch = this.getChannel();

        // 默认取当前泳道；传入 lane 则使用传入值
        const effectiveLane =
            lane !== undefined ? (lane === 'prod' ? undefined : lane) : getLane();
        const actualRK = laneRK(routingKey, effectiveLane);

        const msgHeaders: Record<string, unknown> = { ...headers };
        if (delayMs !== undefined) {
            msgHeaders['x-delay'] = delayMs;
        }

        ch.publish(EXCHANGE_NAME, actualRK, Buffer.from(JSON.stringify(body)), {
            persistent: true,
            contentType: 'application/json',
            headers: Object.keys(msgHeaders).length > 0 ? msgHeaders : undefined,
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
        this.getChannel().ack(msg);
    }

    nack(msg: ConsumeMessage, requeue = false): void {
        this.getChannel().nack(msg, false, requeue);
    }

    getChannel(): Channel {
        if (!this.channel) {
            throw new Error('RabbitMQ channel not available; call connect() first');
        }
        return this.channel;
    }

    async close(): Promise<void> {
        try {
            await this.channel?.close();
            await this.conn?.close();
        } catch {
            // ignore close errors
        }
        this.channel = null;
        this.conn = null;
    }

    private scheduleReconnect(): void {
        if (this.reconnecting) return;
        this.reconnecting = true;
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
