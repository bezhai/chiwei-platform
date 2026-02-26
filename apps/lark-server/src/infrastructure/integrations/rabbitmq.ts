import amqplib, { Channel, ChannelModel, ConsumeMessage } from 'amqplib';

const EXCHANGE_NAME = 'post_processing';
const DLX_NAME = 'post_processing_dlx';
const DLQ_NAME = 'dead_letters';

export const QUEUE_RECALL = 'recall';
export const RK_RECALL = 'action.recall';

export type MessageHandler = (msg: ConsumeMessage) => Promise<void>;

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

        // DLX + DLQ
        await ch.assertExchange(DLX_NAME, 'fanout', { durable: true });
        await ch.assertQueue(DLQ_NAME, { durable: true });
        await ch.bindQueue(DLQ_NAME, DLX_NAME, '');

        // Main exchange (delayed-message)
        await ch.assertExchange(EXCHANGE_NAME, 'x-delayed-message', {
            durable: true,
            arguments: { 'x-delayed-type': 'topic' },
        });

        // recall queue
        await ch.assertQueue(QUEUE_RECALL, {
            durable: true,
            arguments: { 'x-dead-letter-exchange': DLX_NAME },
        });
        await ch.bindQueue(QUEUE_RECALL, EXCHANGE_NAME, RK_RECALL);

        console.info('[RabbitMQ] topology declared');
    }

    async publish(
        routingKey: string,
        body: Record<string, unknown>,
        delayMs?: number,
        headers?: Record<string, unknown>,
    ): Promise<void> {
        const ch = this.getChannel();
        const msgHeaders: Record<string, unknown> = { ...headers };
        if (delayMs !== undefined) {
            msgHeaders['x-delay'] = delayMs;
        }

        ch.publish(EXCHANGE_NAME, routingKey, Buffer.from(JSON.stringify(body)), {
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
