/**
 * lark-service 进程入口：把真实的基础设施单例接到启动序列上，然后开三个入站入口。
 *
 * 这个文件是唯一持有全局单例的地方。别的模块都只接口子（见 startup.ts 的
 * LarkBackends、server/app.ts 的 BotRoster、lark/inbound.ts 的 LarkInboundPorts），
 * 这样启动顺序、HTTP 装配和整条入站链都能在没有 PG / Redis / MQ / Mongo 的情况下测。
 */

import type { Document } from 'mongodb';
import { LaneRouter } from '@inner/shared';
import { botDirectory } from '@inner/shared/bot';
import { getRedisClient, resetRedisClient } from '@inner/shared/cache';
import { getLaneBindingResolver } from '@inner/shared/lane-binding';
import { getMongoService, resetMongoService } from '@inner/shared/mongo';
import { getLane, rabbitmqClient } from '@inner/shared/mq';
import { NotBlocked } from '@inner/shared/rules';

import { loadConfig } from './config';
import { assembleLarkAttachments, type LarkAttachmentCache } from './lark/attachments';
import { larkAppIdOf } from './lark/bot-lookup';
import { httpAiProviderAccount } from './lark/commands/ai-provider';
import { cachedMemeTemplates, httpMemes } from './lark/commands/memes';
import { toolServiceKeywords } from './lark/commands/word-cloud';
import { LARK_CHANNEL } from './lark/channel';
import { larkCredentials } from './lark/credentials';
import { postgresEmojiCatalog, type LarkEmojiCatalog } from './lark/emoji/catalog';
import { httpEmojiSource, syncLarkEmojis } from './lark/emoji/sync';
import { createLarkInbound, type LarkInbound } from './lark/inbound';
import {
    handOffToInboundLane,
    inboundLaneDispatchEnabled,
} from './lark/ingress/lane-handoff';
import { holdsLarkWebSockets, type LarkWebSockets } from './lark/ingress/websocket';
import { loadLarkPersonaNames } from './lark/persona-names';
import { handleLarkCardAction } from './lark/photo/callback';
import { dailyNewPhoto, dailyPhoto } from './lark/photo/daily';
import { localPixivMirror } from './lark/photo/pixiv-mirror';
import { readyPhotos } from './lark/photo/ready';
import { toolServiceResize } from './lark/photo/resize';
import {
    projectLarkInbound,
    type LarkInboundDeps,
} from './lark/projection/inbound-projection';
import { larkMessageLock, redisMessageLockStore } from './lark/projection/message-lock';
import { postgresLarkTables } from './lark/projection/postgres-tables';
import type { LarkStore } from './lark/projection/tables';
import { createSdkLarkApi, larkClientPool } from './lark/outbound/sdk-lark-api';
import { receiveLarkMessage } from './lark/receive-message';
import { receiveLarkRecall } from './lark/recall-message';
import { redisRepeatCounter } from './lark/repeat/counter';
import {
    larkCommands,
    type LarkCommandCache,
    type LarkCommandDeps,
} from './lark/rules/commands';
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
import { register } from './server/metrics';

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
        // 交接走内网 HTTP：路由器按目标泳道注 x-ctx-lane，sidecar 解析目标；泳道的
        // Service 不存在时它把请求打回 prod 自己，那一支由本进程的交接端点接住。
        // INNER_HTTP_SECRET 由 loadConfig 在启动期保证存在，这里不兜底 —— 兜成空串
        // 只会让接收端回 401，比起不来更难查。
        handOffToLane: (envelope) =>
            handOffToInboundLane(
                { fetcher: laneRouter(), innerSecret: process.env.INNER_HTTP_SECRET as string },
                envelope,
            ),
        withMessageLock: larkMessageLock(redisMessageLockStore(getRedisClient)),
    };
}

/**
 * 打下游服务的那个路由器。**整个进程一个**：它自带 30s 的注册表轮询，每处各建一个
 * 就是每处多一个永不停的定时器。
 *
 * 走 LaneRouter 而不是裸 fetch/axios，是为了拿到它按请求上下文注入 `x-ctx-lane` 的
 * 那一层 —— 泳道信封进来的消息靠它路由到本泳道的下游。
 */
let router: LaneRouter | undefined;
function laneRouter(): LaneRouter {
    router ??= new LaneRouter(
        process.env.REGISTRY_URL || 'http://lite-registry:8080',
        30_000,
        register,
    );
    return router;
}

/** 入站附件缓存的真实装配：一个打 tool-service 的客户端，加两件本 pod 的事实。 */
function realAttachments(): LarkAttachmentCache {
    const toolService = laneRouter().createClient('tool-service');

    return assembleLarkAttachments({
        post: (path, body, headers) => toolService.post(path, body, { headers }),
        innerSecret: process.env.INNER_HTTP_SECRET,
        // pod 的静态泳道。webhook 直接打进泳道时请求上下文里没有 lane（gateway 不注
        // 那个头），这才是可靠的本泳道标识 —— 文件轨用它，理由见 attachments.ts。
        lane: getLane(),
    });
}

/**
 * 指令层的长命依赖，**整个进程一份**。
 *
 * 这是 Task D 那四批唯一的依赖入口：填一个指令槽位时在 LarkCommandDeps 上加一行、
 * 在这里递一行，指令自己那份实现在自己的文件里（见 lark/rules/commands.ts 的文件头）。
 * 没有这个入口的话，指令 handler 要的飞书客户端和存储就只能来自全局单例。
 *
 * 飞书客户端池按 bot 分，一直留着 —— SDK 客户端内部缓存 tenant access token，每次新建
 * 等于每条消息都去飞书换一次 token。定时任务和卡片回调也从这里取（它们跟指令同进程）。
 */
function realCommandDeps(store: LarkStore, emoji: LarkEmojiCatalog): LarkCommandDeps {
    const bots = botDirectory.getAllBotConfigs().filter((bot) => bot.channel === LARK_CHANNEL);
    const toolService = laneRouter().createClient('tool-service');
    const memes = httpMemes(`${process.env.MEME_HOST}:${process.env.MEME_PORT}`);
    // Redis 上的一个键值对。指令层直接用它，meme 的模板列表缓存也建在它上面。
    const cache: LarkCommandCache = {
        get: (key) => getRedisClient().get(key),
        setWithExpire: async (key, value, seconds) => {
            await getRedisClient().setWithExpire(key, value, seconds);
        },
    };
    const api = createSdkLarkApi(
        larkClientPool(
            bots.map((bot) => ({ botName: bot.bot_name, credentials: larkCredentials(bot) })),
        ),
    );
    return {
        api,
        store,
        emoji,
        // 复读的计数器。**不是**下面那个键值对端口的一个用法：它的读-改-写必须原子，
        // 所以整段跑在 Redis 那边（见 lark/repeat/counter.ts）。
        repeatCounter: redisRepeatCounter(getRedisClient),
        database: larkDataSource(),
        cache,
        // 302.ai 是外部服务，走裸 fetch（LaneRouter 只认本集群内部的服务名）。
        aiProvider: httpAiProviderAccount(process.env.AI_PROVIDER_ADMIN_KEY),
        // 分词在 tool-service，是本集群内部的服务，所以走 LaneRouter 的客户端 ——
        // 泳道里敲「水群」要打到同泳道的 tool-service（有就打，没有 fallback prod）。
        // 客户端建一次：每次请求各建一个 axios 实例等于每次都重装一遍拦截器。
        keywords: toolServiceKeywords((path, body) => toolService.post(path, body)),
        // 表情包服务也是外部的（自己的 host:port，不在注册表里）。模板列表包一层十分钟
        // 缓存，键名与 channel-server 那份逐字相同 —— 切换窗口里两边共用同一个键。
        memes: {
            templates: cachedMemeTemplates(cache, memes.templates),
            render: memes.render,
        },
        // 图库（另一个 Mongo + MinIO）与缩图（tool-service）都在这后面。三个入口 ——
        // 指令、卡片回调、定时任务 —— 共用这一份，所以口径只有一处。
        photos: readyPhotos({
            library: localPixivMirror(),
            resize: toolServiceResize((path, init) =>
                laneRouter().fetch('tool-service', path, init),
            ),
            upload: (bytes) => api.uploadImage(bytes),
        }),
    };
}

/**
 * 规则段的真实装配。接线本身在 lark/rules/inbound-rules.ts（那里能测），这里只负责
 * 把本进程的那几个单例递进去。
 *
 * 去重标记与投影锁共用同一份 Redis 实现：比对持有者再删的那段 Lua 全仓只写一次。
 */
function realRules(commands: LarkCommandDeps, store: LarkStore): LarkRulesDeps {
    return assembleLarkRules({
        // 清单里填好的那些指令，依赖绑上。空槽位不产出规则，所以还欠着的那几批不影响
        // 这一行（见 lark/rules/commands.ts）。
        commands: larkCommands(commands),
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
async function realInbound(commands: LarkCommandDeps, store: LarkStore): Promise<LarkInbound> {
    const personaIds = botDirectory
        .getAllBotConfigs()
        .map((bot) => bot.persona_id)
        .filter((id): id is string => Boolean(id));
    const personaName = await loadLarkPersonaNames(larkDataSource(), personaIds);
    const eventLog = getMongoService().getCollection(LARK_EVENT_COLLECTION);
    const projection = realProjection(store);
    const attachments = realAttachments();
    const rules = realRules(commands, store);

    return createLarkInbound({
        roster: botDirectory,
        // 本进程所在泳道。只用来在交接端点上回报"接住它的是谁"（见 lark/inbound.ts）。
        lane: getLane() ?? 'prod',
        personaName,
        record: (payload) => eventLog.insertOne(payload as Document),
        onMessage: (reading, event) =>
            receiveLarkMessage(
                {
                    project: (r, e) => projectLarkInbound(projection, r, e),
                    cacheAttachments: attachments,
                    applyRules: async (r, recorded, e) => {
                        await applyLarkRules(rules, r, recorded, e);
                    },
                },
                reading,
                event,
            ),
        // 卡片回调复用指令那份依赖：它要的飞书客户端、会话行、取图口径都在里面，
        // 而且必须是**同一个**客户端池（拆两份就是每个 bot 两套 tenant token）。
        onCardAction: (payload) => handleLarkCardAction(commands, payload),
        // 撤回只要两条语句（按 om_id 查映射、标 recalled_at），所以只递库，不递
        // 飞书客户端 —— 这条链一次都不用回头问飞书。
        onRecall: (recall, receivedAt) => receiveLarkRecall({ store }, recall, receivedAt),
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
    // 指令那份长命依赖建一次，规则段 / 卡片回调 / 定时任务三处共用（见 realCommandDeps）。
    const store = postgresLarkTables(larkDataSource());
    // 表情目录**一份**：写端是下面那个每小时的同步任务，读端是复读指令。两处各建一个
    // 也能跑，但那样"这张表有哪两个动作"就不再是一处能看全的事。
    const emoji = postgresEmojiCatalog(larkDataSource());
    const commands = realCommandDeps(store, emoji);
    const inbound = await realInbound(commands, store);

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
    //
    // 入口三：泳道交接的接收端点，跟 webhook 一起挂在这个 app 上（见 server/app.ts）。
    // **每个部署都挂，prod 也挂** —— 泳道的 Service 不存在时 sidecar 把交接打回 prod
    // 自己，那一支就由 prod 上的这个端点接住。
    const app = createLarkServiceApp({
        bots: botDirectory,
        inbound,
        ingress: () => sockets?.status() ?? NO_WEBSOCKETS,
    });
    Bun.serve({ port: config.port, fetch: app.fetch });

    // 定时任务归这个进程，不归 lark-outbound：出站可以多副本，每个副本各起一份 cron
    // 就是往那个写死的飞书群发 N 遍日报、按小时重复覆写同一张共享表（见 schedule.ts）。
    // key 必须与清单里的任务名逐字对上，对不上装配期就抛。
    const daily = { api: commands.api, photos: commands.photos, wait: Bun.sleep, now: () => new Date() };
    schedules = startLarkSchedules({
        runs: {
            'daily-photo': dailyPhoto(daily),
            'daily-new-photo': dailyNewPhoto(daily),
            'emoji-sync': syncLarkEmojis({ source: httpEmojiSource(), catalog: emoji }),
        },
        schedule: nodeCronScheduler,
    });

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
