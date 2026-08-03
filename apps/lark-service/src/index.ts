/**
 * lark-service 进程入口：把真实的基础设施单例接到启动序列上，然后开三个入口。
 *
 * 这个文件是唯一持有全局单例的地方。别的模块都只接口子（见 startup.ts 的
 * LarkBackends、server/app.ts 的 BotRoster、lark/inbound.ts 的 LarkInboundPorts），
 * 这样启动顺序、HTTP 装配和整条入站链都能在没有 PG / Redis / MQ / Mongo 的情况下测。
 */

import type { Document } from 'mongodb';
import { botDirectory } from '@inner/shared/bot';
import { getRedisClient, resetRedisClient } from '@inner/shared/cache';
import { getMongoService, resetMongoService } from '@inner/shared/mongo';
import { getLane, rabbitmqClient } from '@inner/shared/mq';

import { loadConfig } from './config';
import { createLarkInbound, type LarkInbound } from './lark/inbound';
import { holdsLarkWebSockets, type LarkWebSockets } from './lark/ingress/websocket';
import { loadLarkPersonaNames } from './lark/persona-names';
import { larkDataSource } from './ormconfig';
import { bootLarkService, shutdownLarkService, type LarkBackends } from './startup';
import { createLarkServiceApp } from './server/app';

const LARK_EVENT_COLLECTION = 'lark_event';

function realBackends(): LarkBackends {
    return {
        database: larkDataSource(),
        bots: botDirectory,
        cache: {
            ping: () => getRedisClient().getNativeClient().ping(),
            close: () => resetRedisClient(),
        },
        broker: rabbitmqClient,
        eventLog: {
            open: () => getMongoService().initialize(),
            close: () => resetMongoService(),
        },
    };
}

/**
 * 飞书入站的真实装配。必须在 bootLarkService 之后调用：人设名要查库，bot 目录也
 * 得先加载完。
 */
async function realInbound(): Promise<LarkInbound> {
    const personaIds = botDirectory
        .getAllBotConfigs()
        .map((bot) => bot.persona_id)
        .filter((id): id is string => Boolean(id));
    const personaName = await loadLarkPersonaNames(larkDataSource(), personaIds);
    const eventLog = getMongoService().getCollection(LARK_EVENT_COLLECTION);

    return createLarkInbound({
        roster: botDirectory,
        personaName,
        record: (payload) => eventLog.insertOne(payload as Document),
        onMessage: async (reading) => {
            // 本段到解析为止。common 投影、规则与指令、发 chat.request 由后续段接在
            // 这里 —— 在那之前这条日志是"事件走通了哪个入口、解析成了什么"的唯一现场。
            console.info(
                `[lark-inbound] parsed ${reading.message.messageType} message ` +
                    `${reading.message.messageId} in chat ${reading.message.chatId}: ` +
                    `${reading.content.length} part(s), ` +
                    `${reading.mentions.all.length} mention(s)`,
            );
        },
    });
}

/** 不持有长连的部署（webhook-only、泳道部署）没有连接要报，就绪判据里 expected=0。 */
const NO_WEBSOCKETS = { expected: 0, connected: 0, bots: [] };

function handleSignals(backends: LarkBackends, closeWebSockets: () => void): void {
    for (const signal of ['SIGINT', 'SIGTERM'] as const) {
        process.on(signal, async () => {
            console.info(`[lark-service] ${signal} received, shutting down`);
            closeWebSockets();
            await shutdownLarkService(backends);
            process.exit(0);
        });
    }

    process.on('uncaughtException', (error) => {
        console.error('[lark-service] uncaught exception:', error);
        process.exit(1);
    });

    process.on('unhandledRejection', (reason) => {
        console.error('[lark-service] unhandled rejection:', reason);
        process.exit(1);
    });
}

async function main(): Promise<void> {
    const config = loadConfig();
    const backends = realBackends();

    await bootLarkService(backends);
    const inbound = await realInbound();

    // 入口二：长连。主动，而且**会跟别的进程抢** —— 飞书对同一 app_id 的多个长连是
    // 随机投递。gate 保证只有 prod 部署并显式打开时才连（见 websocket.ts）。
    //
    // 先于 Bun.serve 开：这样 /api/ready 从第一次被问起就说得出真实连接状态，不会有
    // 一段"还没开始连，却报就绪"的窗口。开的过程不阻塞 —— 连不上时进程照样起来，
    // 由 /api/ready 报 not-ready，而不是让 Pod 起不来。
    let sockets: LarkWebSockets | undefined;
    if (holdsLarkWebSockets()) {
        sockets = await inbound.openWebSockets();
    }
    handleSignals(backends, () => sockets?.close());

    // 入口一：webhook。被动 —— 路由注册上了不代表有流量，实际指向哪个服务由
    // api-gateway 的规则决定。
    const app = createLarkServiceApp({
        bots: botDirectory,
        inbound,
        ingress: () => sockets?.status() ?? NO_WEBSOCKETS,
    });
    Bun.serve({ port: config.port, fetch: app.fetch });

    // 入口三：泳道信封队列。只有泳道部署才消费；prod 不消费（prod 是投递方）。
    // ⚠️ RabbitMQ 是竞争消费：同一条泳道上如果 channel-server 也还订阅着这个队列，
    // 消息会被两边各分走一半。谁订阅是切换动作的一部分，代码保证不了。
    const lane = getLane();
    if (lane) {
        await inbound.consumeLane(lane);
    }

    const bots = botDirectory.getAllBotConfigs();
    console.info(
        `[lark-service] listening on :${config.port} for ${bots.length} lark bot(s): ` +
            (bots.map((b) => b.bot_name).join(', ') || '(none)'),
    );
}

main().catch((error) => {
    console.error('[lark-service] failed to start:', error);
    process.exit(1);
});
