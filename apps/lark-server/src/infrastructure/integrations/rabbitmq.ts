import amqplib, { Channel, ChannelModel, ConsumeMessage } from 'amqplib';

const EXCHANGE_NAME = 'post_processing';
const DLX_NAME = 'post_processing_dlx';
const DLQ_NAME = 'dead_letters';

export const QUEUE_RECALL = 'recall';
export const QUEUE_VECTORIZE = 'vectorize';
export const RK_RECALL = 'action.recall';
export const RK_VECTORIZE = 'task.vectorize';

const NON_PROD_EXPIRES_MS = 86_400_000;

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

        // 非 prod 队列额外参数
        const extraArgs: Record<string, unknown> = lane
            ? { 'x-expires': NON_PROD_EXPIRES_MS }
            : {};
        const baseArgs = { 'x-dead-letter-exchange': DLX_NAME, ...extraArgs };

        // recall queue
        const recallQ = laneQueue(QUEUE_RECALL, lane);
        await ch.assertQueue(recallQ, { durable: true, arguments: baseArgs });
        await ch.bindQueue(recallQ, EXCHANGE_NAME, laneRK(RK_RECALL, lane));

        // vectorize queue
        const vectorizeQ = laneQueue(QUEUE_VECTORIZE, lane);
        await ch.assertQueue(vectorizeQ, { durable: true, arguments: baseArgs });
        await ch.bindQueue(vectorizeQ, EXCHANGE_NAME, laneRK(RK_VECTORIZE, lane));

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
                console.info('[RabbitMQ] reconnected');
            } catch (err) {
                console.error('[RabbitMQ] reconnect failed:', err);
                this.scheduleReconnect();
            }
        }, 5000);
    }
}

export const rabbitmqClient = RabbitMQClient.getInstance();
