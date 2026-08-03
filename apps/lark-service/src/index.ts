/**
 * lark-service 进程入口：把真实的基础设施单例接到启动序列上，然后开 HTTP。
 *
 * 这个文件是唯一持有全局单例的地方。别的模块都只接口子（见 startup.ts 的
 * LarkBackends、server/app.ts 的 BotRoster），这样启动顺序和 HTTP 装配都能在没有
 * PG / Redis / MQ 的情况下测。
 */

import { botDirectory } from '@inner/shared/bot';
import { getRedisClient, resetRedisClient } from '@inner/shared/cache';
import { rabbitmqClient } from '@inner/shared/mq';

import { loadConfig } from './config';
import { larkDataSource } from './ormconfig';
import { bootLarkService, shutdownLarkService, type LarkBackends } from './startup';
import { createLarkServiceApp } from './server/app';

function realBackends(): LarkBackends {
    return {
        database: larkDataSource(),
        bots: botDirectory,
        cache: {
            ping: () => getRedisClient().getNativeClient().ping(),
            close: () => resetRedisClient(),
        },
        broker: rabbitmqClient,
    };
}

function handleSignals(backends: LarkBackends): void {
    for (const signal of ['SIGINT', 'SIGTERM'] as const) {
        process.on(signal, async () => {
            console.info(`[lark-service] ${signal} received, shutting down`);
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

    handleSignals(backends);
    await bootLarkService(backends);

    const app = createLarkServiceApp({ bots: botDirectory });
    Bun.serve({ port: config.port, fetch: app.fetch });

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
