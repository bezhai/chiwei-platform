import type { Hono } from 'hono';
import type { BotConfig } from '@entities/bot-config';
import type { InboundLaneEnvelope } from '@integrations/inbound-lane';

export interface ChannelRuntime {
    channel: string;
    initialize?(): Promise<void>;
    runInitializers?(): Promise<void>;
    registerHttpIngress?(app: Hono, bots: BotConfig[]): Promise<void> | void;
    startDirectIngress?(bots: BotConfig[]): Promise<void>;
    handleInboundLaneEnvelope?(env: InboundLaneEnvelope): Promise<void>;
    shutdown?(): Promise<void> | void;
}

const runtimes = new Map<string, ChannelRuntime>();

export function registerChannelRuntime(runtime: ChannelRuntime): void {
    if (runtimes.has(runtime.channel)) {
        throw new Error(
            `channel runtime "${runtime.channel}" already registered; duplicate runtime registration`,
        );
    }
    runtimes.set(runtime.channel, runtime);
}

export function getChannelRuntime(channel: string): ChannelRuntime {
    const runtime = runtimes.get(channel);
    if (!runtime) {
        throw new Error(
            `unknown channel runtime "${channel}"; no runtime registered (check plugins/index.ts)`,
        );
    }
    return runtime;
}

export function channelRuntimes(): ChannelRuntime[] {
    return [...runtimes.values()];
}

export async function initializeChannelRuntimes(): Promise<void> {
    for (const runtime of channelRuntimes()) {
        await runtime.initialize?.();
    }
}

export async function runChannelInitializers(): Promise<void> {
    for (const runtime of channelRuntimes()) {
        await runtime.runInitializers?.();
    }
}

export async function registerChannelHttpIngresses(
    app: Hono,
    bots: BotConfig[],
): Promise<void> {
    for (const runtime of channelRuntimes()) {
        await runtime.registerHttpIngress?.(
            app,
            bots.filter((bot) => bot.channel === runtime.channel),
        );
    }
}

export async function startChannelDirectIngresses(bots: BotConfig[]): Promise<void> {
    for (const runtime of channelRuntimes()) {
        await runtime.startDirectIngress?.(
            bots.filter((bot) => bot.channel === runtime.channel),
        );
    }
}

export async function handleInboundLaneEnvelope(env: InboundLaneEnvelope): Promise<void> {
    const runtime = getChannelRuntime(env.channel ?? 'lark');
    if (!runtime.handleInboundLaneEnvelope) {
        throw new Error(
            `channel runtime "${runtime.channel}" cannot handle inbound lane envelopes`,
        );
    }
    await runtime.handleInboundLaneEnvelope(env);
}

export async function shutdownChannelRuntimes(): Promise<void> {
    for (const runtime of channelRuntimes()) {
        await runtime.shutdown?.();
    }
}
