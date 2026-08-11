/**
 * lark-outbound 进程入口：消费飞书的 `chat_response`，把赤尾说的话送出去。
 *
 * ## 为什么它不跟入口进程住在一起
 *
 * 两者的部署策略天然冲突。飞书 WS 长连对同一个 app_id 是随机投递，两个进程同时连
 * 着会静默分流，所以持长连的那个 Deployment 只能 `replicas=1` + `Recreate` ——
 * 滚动更新会有一段两个 Pod 都连着的时间。出站恰恰相反：它是竞争消费，天然可以多
 * 副本、可以滚动更新、崩一个不影响别的。
 *
 * 塞进一个进程，等于让出站也继承"单副本 + 停机式部署"：改一行渲染逻辑要停整个飞书
 * 入口，出站的一次 OOM 会带走长连。这不是资源账能抵消的。
 *
 * ## 这个文件只做装配
 *
 * 业务在 lark/outbound/ 下，一个单例都不认识（见 deliver.ts 的 LarkDeliveryDeps、
 * response-queue.ts 的 LarkResponseConsumerDeps）。所以整条出站链能在一台连不到
 * PG / Redis / MQ / 飞书的机器上跑完。
 */

import { createServer } from 'node:http';
import type { ConsumeMessage } from 'amqplib';
import { Histogram, Registry, collectDefaultMetrics } from 'prom-client';
import { botDirectory } from '@inner/shared/bot';
import { getRedisClient, resetRedisClient } from '@inner/shared/cache';
import { getLane, rabbitmqClient, type Route } from '@inner/shared/mq';

import { loadOutboundConfig } from './config';
import { larkDisplayNameOf, type LarkPersonaName } from './lark/bot-lookup';
import { LARK_CHANNEL } from './lark/channel';
import { larkCredentials } from './lark/credentials';
import { larkSpeakAs } from './lark/outbound/bot-context';
import { deliverLarkChatResponse, type LarkDeliveryDeps } from './lark/outbound/deliver';
import { redisImageRegistry } from './lark/outbound/redis-image-registry';
import { postgresLarkResponseLedger } from './lark/outbound/postgres-ledger';
import { postgresLarkOutboundTables } from './lark/outbound/postgres-tables';
import { postgresLarkGroupRoster } from './lark/outbound/postgres-roster';
import {
    larkBotAliases,
    createLarkMentionResolver,
    withRosterCache,
} from './lark/outbound/mentions';
import { createLarkPostRenderer } from './lark/outbound/render';
import { larkChatResponseBinding } from './lark/outbound/response-queue';
import {
    larkOutboundConsumeSwitch,
    LarkOutboundSubscriptions,
} from './lark/outbound/subscription';
import { createSdkLarkApi, larkClientPool } from './lark/outbound/sdk-lark-api';
import { loadLarkPersonaNames } from './lark/persona-names';
import { larkDataSource } from './ormconfig';
import { bootLarkService, shutdownLarkService, type LarkBackends } from './startup';

/**
 * 群花名册的缓存时长。
 *
 * 一条回复有好几段要渲染，每段都去查一次群成员就是每段一次带 join 的查询。有效期
 * 短是刻意的：群成员变动之后最多迟一个周期就跟上，不需要失效通知。
 */
const ROSTER_CACHE_MS = 30_000;

/**
 * 重读消费开关的间隔。
 *
 * 移交是人工触发的操作步骤，秒级足够；短了纯属白打 paas-engine（DynamicConfig 自己
 * 还有 10s 缓存）。它决定的是"翻开关之后多久真的开始消费"，而 drain 屏障的另一侧
 * （channel-server 那个 worker）用的是同一个数量级。
 */
const SWITCH_POLL_MS = 15_000;

const metrics = new Registry();
collectDefaultMetrics({ register: metrics });

// 阶段标签沿用拆分前 chat-response-worker 的口径，切流时新旧两条路径的曲线才能
// 直接对比。
const deliveryDuration = new Histogram({
    name: 'chat_response_duration_seconds',
    help: 'Duration of each lark outbound stage',
    labelNames: ['stage'] as const,
    buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
    registers: [metrics],
});

const queueDelay = new Histogram({
    name: 'chat_response_queue_delay_seconds',
    help: 'Time a lark chat_response spent waiting in the queue',
    buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
    registers: [metrics],
});

function backends(): LarkBackends {
    return {
        database: larkDataSource(),
        bots: botDirectory,
        cache: {
            ping: () => getRedisClient().getNativeClient().ping(),
            close: () => resetRedisClient(),
        },
        broker: rabbitmqClient,
        // 出站没有 eventLog：原始报文在入口那一侧第一次进来时就记过了（见 startup.ts）。
    };
}

/**
 * 出站的真实装配。必须在 bootLarkService 之后调用：人设名要查库，bot 目录也得先
 * 加载完，飞书客户端池要拿每个 bot 的凭据。
 */
async function realDelivery(): Promise<LarkDeliveryDeps> {
    const dataSource = larkDataSource();
    const bots = botDirectory.getAllBotConfigs().filter((bot) => bot.channel === LARK_CHANNEL);
    const personaName: LarkPersonaName = await loadLarkPersonaNames(
        dataSource,
        bots.map((bot) => bot.persona_id).filter((id): id is string => Boolean(id)),
    );

    const api = createSdkLarkApi(
        larkClientPool(
            bots.map((bot) => ({ botName: bot.bot_name, credentials: larkCredentials(bot) })),
        ),
    );

    // 花名册缓存归装配根持有，不是模块级全局：谁在用一眼可见。
    const roster = withRosterCache(postgresLarkGroupRoster(dataSource), ROSTER_CACHE_MS);

    return {
        store: postgresLarkOutboundTables(dataSource),
        ledger: postgresLarkResponseLedger(dataSource),
        api,
        render: createLarkPostRenderer({
            mentions: createLarkMentionResolver({
                roster,
                // 每次都重算：common_user_id 和人设名都是启动时回填的，缓存一份
                // 快照会把回填之前的空值一直留着。目录规模是个位数。
                aliases: () => larkBotAliases(botDirectory, personaName),
            }),
            images: {
                registry: redisImageRegistry({
                    hgetall: (key) => getRedisClient().hgetall(key),
                }),
                uploader: api,
            },
        }),
        botCommonUserId: (botName) => botDirectory.getBotCommonUserId(botName),
        botDisplayName: (botName) => larkDisplayNameOf(botDirectory, personaName, botName),
        // 时间有序的 uuid v7：它同时是主键和"这条消息什么时候发的"的排序依据。
        newCommonId: () => Bun.randomUUIDv7(),
        now: () => Date.now(),
        wait: (ms) => Bun.sleep(ms),
        speakAs: larkSpeakAs,
        observe: (stage, seconds) => deliveryDuration.labels({ stage }).observe(seconds),
    };
}

function handleSignals(): void {
    for (const signal of ['SIGINT', 'SIGTERM'] as const) {
        process.on(signal, async () => {
            console.info(`[lark-outbound] ${signal} received, shutting down`);
            await shutdownLarkService(backends());
            process.exit(0);
        });
    }

    process.on('uncaughtException', (error) => {
        console.error('[lark-outbound] uncaught exception:', error);
        process.exit(1);
    });

    process.on('unhandledRejection', (reason) => {
        console.error('[lark-outbound] unhandled rejection:', reason);
        process.exit(1);
    });
}

async function main(): Promise<void> {
    const config = loadOutboundConfig();
    await bootLarkService(backends());
    handleSignals();

    // 预热：DB / bot 目录 / 人设名 / 飞书客户端池全部就位。**在订阅之前**做完 ——
    // 交接窗口里"进程起来了但还没连上后端"的那段时延就是队列没人消费的空窗，而泳道
    // 队列带 10s TTL，堆过去就被 DLX 弹回 prod 由 prod 实例发出去。
    const delivery = await realDelivery();

    const amqp = {
        declareRoute: (route: Route) => rabbitmqClient.declareRoute(route),
        consume: (name: string, handler: (msg: ConsumeMessage) => Promise<void>) =>
            rabbitmqClient.consume(name, handler),
        drainConsumer: (name: string) => rabbitmqClient.drainConsumer(name),
        ack: (msg: ConsumeMessage) => rabbitmqClient.ack(msg),
        nack: (msg: ConsumeMessage, requeue: boolean) => rabbitmqClient.nack(msg, requeue),
    };

    // 声明用的泳道和订阅用的泳道必须同源：declareRoute 内部也读 env 的 LANE。
    // 声明的是 A、订阅的是 B 的话，两步都"成功"，就是一条消息都收不到。
    //
    // 两条队列一把开关：channel-server 那侧释放它们用的就是同一把 key，接管侧分成两把
    // 就会出现「只翻了一把，另一条队列没有任何消费者」的窗口（见 subscription.ts）。
    const subscription = new LarkOutboundSubscriptions({
        amqp,
        lane: getLane(),
        queues: [
            larkChatResponseBinding({
                amqp,
                deliver: (response, lane) => deliverLarkChatResponse(delivery, response, lane),
                observeQueueDelay: (seconds) => queueDelay.observe(seconds),
            }),
        ],
        readConsumeSwitch: larkOutboundConsumeSwitch,
    });

    // 判断这个 Deployment 活得好不好靠队列积压和处理时延，不靠健康检查接口 ——
    // 出站是竞争消费，"进程还在"跟"消息在被处理"是两件事。metrics 在订阅之前就起来：
    // 开关还关着的时候它也该是可观测的。
    createServer(async (_req, res) => {
        res.setHeader('Content-Type', metrics.contentType);
        res.end(await metrics.metrics());
    }).listen(config.metricsPort, () => {
        console.info(`[lark-outbound] metrics on :${config.metricsPort}`);
    });

    // 开关翻动在运行期生效，两个方向都不必重启：翻开就地增订，翻回去走 drain 屏障
    // 把队列交还（见 LarkOutboundSubscriptions）。
    await subscription.reconcile();
    setInterval(() => {
        subscription.reconcile().catch((error) => {
            console.error('[lark-outbound] reconcile failed:', error);
        });
    }, SWITCH_POLL_MS).unref();

    const names = botDirectory.getAllBotConfigs().map((bot) => bot.bot_name);
    const queues = subscription.subscribedQueues();
    const consuming =
        queues.length > 0
            ? queues.join(', ')
            : `(nothing yet — the switch is off; re-read every ${SWITCH_POLL_MS / 1000}s, ` +
              'no restart needed)';
    console.info(
        `[lark-outbound] up for ${names.length} lark bot(s): ${names.join(', ') || '(none)'}; ` +
            `consuming ${consuming}`,
    );
}

main().catch((error) => {
    console.error('[lark-outbound] failed to start:', error);
    process.exit(1);
});
