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
import { getLaneBindingResolver } from '@inner/shared/lane-binding';
import { getMongoService, resetMongoService } from '@inner/shared/mongo';
import { getLane, rabbitmqClient } from '@inner/shared/mq';
import { NotBlocked } from '@inner/shared/rules';

import { loadConfig } from './config';
import { larkAppIdOf } from './lark/bot-lookup';
import { createLarkInbound, type LarkInbound } from './lark/inbound';
import {
    handOffOverRabbit,
    inboundLaneDispatchEnabled,
} from './lark/ingress/lane-handoff';
import { holdsLarkWebSockets, type LarkWebSockets } from './lark/ingress/websocket';
import { loadLarkPersonaNames } from './lark/persona-names';
import {
    projectLarkInbound,
    type LarkInboundDeps,
} from './lark/projection/inbound-projection';
import { larkMessageLock, redisMessageLockStore } from './lark/projection/message-lock';
import { postgresLarkTables } from './lark/projection/postgres-tables';
import type { LarkStore } from './lark/projection/tables';
import { receiveLarkMessage } from './lark/receive-message';
import {
    applyLarkRules,
    assembleLarkRules,
    type LarkRulesDeps,
} from './lark/rules/inbound-rules';
import { larkDataSource } from './ormconfig';
import {
    nodeCronScheduler,
    startLarkSchedules,
    type LarkSchedules,
} from './schedule';
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
 * 投影的真实装配：库、Redis 锁、泳道绑定、MQ 全在这里接上，投影本身一个单例都不
 * 认识（见 lark/projection/inbound-projection.ts 的 LarkInboundDeps）。
 */
function realProjection(store: LarkStore): LarkInboundDeps {
    return {
        store,
        // 时间有序的 uuid v7：它同时是主键和"这条消息什么时候来的"的排序依据。
        newCommonId: () => Bun.randomUUIDv7(),
        appIdOfBot: (botName) => larkAppIdOf(botDirectory, botName),
        // 本进程所在泳道来自部署环境，不是消息上下文 —— 分叉判断问的是"这条消息该
        // 不该留在我这儿"。
        currentLane: getLane() ?? 'prod',
        laneDispatchEnabled: inboundLaneDispatchEnabled,
        laneOf: (channel, botGlobalId, commonConversationId) =>
            getLaneBindingResolver().resolveLane(channel, botGlobalId, commonConversationId),
        handOffToLane: handOffOverRabbit,
        withMessageLock: larkMessageLock(redisMessageLockStore(getRedisClient)),
    };
}

/**
 * 规则段的真实装配。接线本身在 lark/rules/inbound-rules.ts（那里能测），这里只负责
 * 把本进程的那几个单例递进去。
 *
 * 去重标记与投影锁共用同一份 Redis 实现：比对持有者再删的那段 Lua 全仓只写一次。
 */
function realRules(store: LarkStore): LarkRulesDeps {
    return assembleLarkRules({
        bots: botDirectory,
        store,
        marker: redisMessageLockStore(getRedisClient),
        broker: rabbitmqClient,
        notBlocked: NotBlocked,
    });
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
    const store = postgresLarkTables(larkDataSource());
    const projection = realProjection(store);
    const rules = realRules(store);

    return createLarkInbound({
        roster: botDirectory,
        personaName,
        record: (payload) => eventLog.insertOne(payload as Document),
        onMessage: (reading, event) =>
            receiveLarkMessage(
                {
                    project: (r, e) => projectLarkInbound(projection, r, e),
                    applyRules: async (r, p, e) => {
                        await applyLarkRules(rules, r, p, e);
                    },
                },
                reading,
                event,
            ),
    });
}

/** 不持有长连的部署（webhook-only、泳道部署）没有连接要报，就绪判据里 expected=0。 */
const NO_WEBSOCKETS = { expected: 0, connected: 0, bots: [] };

/**
 * `quiesce` 停掉本进程自己产生工作的那几样（长连、定时任务），**在关后端连接之前** ——
 * 反过来的话，正好在这一刻触发的定时任务会拿着一个正在关的 DB 连接去写库。
 */
function handleSignals(backends: LarkBackends, quiesce: () => void): void {
    for (const signal of ['SIGINT', 'SIGTERM'] as const) {
        process.on(signal, async () => {
            console.info(`[lark-service] ${signal} received, shutting down`);
            quiesce();
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
    let schedules: LarkSchedules | undefined;
    handleSignals(backends, () => {
        sockets?.close();
        schedules?.stop();
    });

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

    // 定时任务归这个进程，不归 lark-outbound：出站可以多副本，每个副本各起一份 cron
    // 就是往那个写死的飞书群发 N 遍日报（见 schedule.ts）。三个槽位现在都还欠着，
    // 所以这里挂不上任何东西 —— D2 / D3 各自往 runs 里加一个同名的本体。
    schedules = startLarkSchedules({ runs: {}, schedule: nodeCronScheduler });

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
