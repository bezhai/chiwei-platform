/**
 * qq-gateway 入口：装配各模块。入站走 WebSocket 主动长连接，出站走 HTTP。
 *
 * 数据流：
 *   入站：QQ gateway --ws--> QQGatewayClient → 归一化 → LaneRouter POST channel-server /api/internal/qq/inbound
 *   出站：channel-server → POST /qq/outbound → 被动窗口 reserve → QQ api 发文本
 */

import Redis from 'ioredis';
import { SQL } from 'bun';
import { LaneRouter } from '@inner/shared/lane-router';
import { loadConfig } from './config';
import { QQClient, type QQLogger } from './qq/api';
import { loadQQCredentials, type CredentialsQuery } from './qq/credentials';
import { QQGatewayClient, type GatewayWebSocket } from './qq/gateway-client';
import { PassiveWindowManager } from './passive-window/manager';
import { RedisPassiveWindowStore, type MinimalRedis } from './passive-window/redis-store';
import { createInboundForwarder } from './server/inbound-forwarder';
import { createQQGatewayApp } from './server/app';

const log: QQLogger = {
    info: (msg) => console.info(msg),
    warn: (msg) => console.warn(msg),
    error: (msg) => console.error(msg),
};

async function main(): Promise<void> {
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

    // QQ 凭据从 bot_config.credentials 读，不再走 env。把 Bun 内置 SQL 客户端包成注入的查询执行器。
    const sql = new SQL({
        hostname: cfg.postgres.host,
        port: cfg.postgres.port,
        username: cfg.postgres.user,
        password: cfg.postgres.password,
        database: cfg.postgres.db,
    });
    const query: CredentialsQuery = (text, params) => sql.unsafe(text, params);
    const { appId, appSecret } = await loadQQCredentials(cfg.botName, { query });

    const qqClient = new QQClient({ appId, clientSecret: appSecret, log });

    const laneRouter = new LaneRouter(cfg.registryUrl);
    const forwardInbound = createInboundForwarder({
        fetcher: laneRouter,
        service: cfg.channelServerService,
        path: cfg.channelServerInboundPath,
        innerSecret: cfg.innerSecret,
        // 自身 lane（PaaS 注入），WS 回调里 forward 无 context，需手动注入才能泳道路由。
        selfLane: process.env.LANE,
        log,
    });

    // ── 入站：WebSocket 主动长连接 ──
    const gatewayClient = new QQGatewayClient({
        botName: cfg.botName,
        getAccessToken: () => qqClient.getAccessToken(),
        getGatewayUrl: () => qqClient.getGatewayUrl(),
        wsFactory: (url) => new WebSocket(url) as unknown as GatewayWebSocket,
        forwardInbound,
        log,
    });
    gatewayClient.start();

    // ── 出站：HTTP /qq/outbound ──
    const { app } = createQQGatewayApp({
        botName: cfg.botName,
        innerSecret: cfg.innerSecret,
        windowManager,
        qqClient,
        log,
    });

    Bun.serve({ port: cfg.port, fetch: app.fetch });
    log.info(`[qq-gateway] bot=${cfg.botName} ws inbound connecting, outbound listening on :${cfg.port}`);
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
