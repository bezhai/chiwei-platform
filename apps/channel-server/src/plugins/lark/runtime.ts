import type { Hono } from 'hono';
import type { BotConfig } from '@entities/bot-config';
import type { InboundLaneEnvelope } from '@integrations/inbound-lane';
import type { ChannelRuntime } from '@plugins/runtime';
import { initializeLarkClients } from '@integrations/lark-client';
import { larkEventHandlers } from '@plugins/lark/events/handlers';
import { LarkEventIngress } from './webhook/ingress';
import { isDirectIngressEnabled } from './webhook/ingress-gate';
import { upsertAllLarkChatInfo } from './group-initializer';

let directIngress: LarkEventIngress | undefined;

export const larkRuntime: ChannelRuntime = {
    channel: 'lark',

    async initialize(): Promise<void> {
        await initializeLarkClients();
    },

    async runInitializers(): Promise<void> {
        if (process.env.NEED_INIT !== 'true') return;
        await upsertAllLarkChatInfo();
    },

    registerHttpIngress(app: Hono, bots: BotConfig[]): void {
        new LarkEventIngress().registerHttpBots(app, bots);
        console.info(`[ingress] lark webhook registered ${bots.length} http bot(s)`);
    },

    async startDirectIngress(bots: BotConfig[]): Promise<void> {
        if (!isDirectIngressEnabled()) return;
        directIngress = new LarkEventIngress();
        await directIngress.startWebSocketBots(bots);
        console.info(`[ingress] direct lark ws ON: started ${bots.length} ws bot(s)`);
    },

    async handleInboundLaneEnvelope(env: InboundLaneEnvelope): Promise<void> {
        await larkEventHandlers.handleMessageReceive(env.params as never);
    },

    shutdown(): void {
        directIngress?.closeWebSocketClients();
        directIngress = undefined;
    },
};
