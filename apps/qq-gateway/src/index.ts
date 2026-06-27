/**
 * qq-gateway 入口：装配各模块并起 HTTP 服务。
 *
 * 数据流：
 *   QQ → POST {webhookPath} → 验签/握手 → 归一化 → LaneRouter POST channel-server /api/internal/qq/inbound
 *   channel-server → POST /qq/outbound → 被动窗口 reserve → QQ api 发文本
 */

import Redis from 'ioredis';
import { LaneRouter } from '@inner/shared/lane-router';
import { loadConfig } from './config';
import { QQClient, type QQLogger } from './qq/api';
import { PassiveWindowManager } from './passive-window/manager';
import { RedisPassiveWindowStore, type MinimalRedis } from './passive-window/redis-store';
import { createInboundForwarder } from './server/inbound-forwarder';
import { createQQGatewayApp } from './server/app';

const log: QQLogger = {
    info: (msg) => console.info(msg),
    warn: (msg) => console.warn(msg),
    error: (msg) => console.error(msg),
};

function main(): void {
    const cfg = loadConfig();

    const redis = new Redis({
        host: cfg.redis.host,
        port: cfg.redis.port,
        password: cfg.redis.password,
        retryStrategy: (times) => Math.min(times * 50, 2000),
        maxRetriesPerRequest: null,
    });

    const store = new RedisPassiveWindowStore(redis as unknown as MinimalRedis);
    const windowManager = new PassiveWindowManager(store, {
        windowMs: cfg.windowMs,
        maxReplies: cfg.maxReplies,
    });

    const qqClient = new QQClient({ appId: cfg.appId, clientSecret: cfg.appSecret, log });

    const laneRouter = new LaneRouter(cfg.registryUrl);
    const forwardInbound = createInboundForwarder({
        fetcher: laneRouter,
        service: cfg.channelServerService,
        path: cfg.channelServerInboundPath,
        innerSecret: cfg.innerSecret,
        log,
    });

    const { app } = createQQGatewayApp({
        botName: cfg.botName,
        botSecret: cfg.botSecret,
        webhookPath: cfg.webhookPath,
        innerSecret: cfg.innerSecret,
        windowManager,
        qqClient,
        forwardInbound,
        log,
    });

    Bun.serve({ port: cfg.port, fetch: app.fetch });
    log.info(`[qq-gateway] bot=${cfg.botName} listening on :${cfg.port}, webhook=${cfg.webhookPath}`);
}

main();
