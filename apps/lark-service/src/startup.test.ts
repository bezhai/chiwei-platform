import { beforeEach, describe, expect, it } from 'bun:test';
import type { BotLoadOptions } from '@inner/shared/bot';

import { LARK_BOT_SCOPE, bootLarkService, shutdownLarkService, type LarkBackends } from './startup';

interface RecordingBackends extends LarkBackends {
    readonly calls: string[];
    readonly botScopes: BotLoadOptions[];
    readonly failing: Set<string>;
}

function backends(): RecordingBackends {
    const calls: string[] = [];
    const botScopes: BotLoadOptions[] = [];
    const failing = new Set<string>();
    let initialized = false;

    const record = (name: string): void => {
        calls.push(name);
        if (failing.has(name)) throw new Error(`${name} exploded`);
    };

    return {
        calls,
        botScopes,
        failing,
        database: {
            initialize: async () => {
                record('database.initialize');
                initialized = true;
            },
            destroy: async () => {
                record('database.destroy');
                initialized = false;
            },
            get isInitialized() {
                return initialized;
            },
        },
        bots: {
            load: async (options) => {
                record('bots.load');
                botScopes.push(options);
            },
        },
        cache: {
            ping: async () => {
                record('cache.ping');
                return 'PONG';
            },
            close: async () => record('cache.close'),
        },
        broker: {
            connect: async () => record('broker.connect'),
            declareTopology: async () => record('broker.declareTopology'),
            close: async () => record('broker.close'),
        },
        eventLog: {
            open: async () => record('eventLog.open'),
            close: async () => record('eventLog.close'),
        },
    };
}

describe('LARK_BOT_SCOPE', () => {
    // 传空数组 BotDirectory 会 fail-closed 抛错，不传等于加载全渠道 —— 那是
    // channel-server 的用法。lark-service 只能持有飞书 bot 的凭据。
    it('names exactly the lark channel', () => {
        expect(LARK_BOT_SCOPE).toEqual({ channels: ['lark'] });
    });
});

describe('bootLarkService', () => {
    let deps: RecordingBackends;

    beforeEach(() => {
        deps = backends();
    });

    it('brings up postgres, bots, redis and rabbitmq in dependency order', async () => {
        await bootLarkService(deps);
        expect(deps.calls).toEqual([
            // bot 目录读 bot_config / common_user，必须在库连上之后
            'database.initialize',
            'bots.load',
            'cache.ping',
            'broker.connect',
            'broker.declareTopology',
            // 审计落库是入站的旁路，但连不上要在启动期就知道，不能等第一条消息
            'eventLog.open',
        ]);
    });

    it('loads only the lark channel bots', async () => {
        await bootLarkService(deps);
        expect(deps.botScopes).toEqual([{ channels: ['lark'] }]);
    });

    it('stops at the first failing backend instead of starting half-connected', async () => {
        deps.failing.add('database.initialize');
        await expect(bootLarkService(deps)).rejects.toThrow('database.initialize exploded');
        expect(deps.calls).toEqual(['database.initialize']);
    });

    it('does not declare the topology when the broker cannot connect', async () => {
        deps.failing.add('broker.connect');
        await expect(bootLarkService(deps)).rejects.toThrow('broker.connect exploded');
        expect(deps.calls).not.toContain('broker.declareTopology');
    });
});

describe('shutdownLarkService', () => {
    let deps: RecordingBackends;

    beforeEach(() => {
        deps = backends();
    });

    it('closes the broker before the stores it feeds', async () => {
        await bootLarkService(deps);
        deps.calls.length = 0;
        await shutdownLarkService(deps);
        expect(deps.calls).toEqual([
            'broker.close',
            'eventLog.close',
            'cache.close',
            'database.destroy',
        ]);
    });

    it('skips postgres when it never came up', async () => {
        await shutdownLarkService(deps);
        expect(deps.calls).toEqual(['broker.close', 'eventLog.close', 'cache.close']);
    });

    // 关停路径上一个失败吞掉其余关闭，就会留下没 quit 的连接，进程 exit 之后在
    // 服务端侧变成僵死连接。每一步各自兜住自己的异常。
    it('keeps closing the rest when one backend fails to close', async () => {
        await bootLarkService(deps);
        deps.calls.length = 0;
        deps.failing.add('broker.close');
        await shutdownLarkService(deps);
        expect(deps.calls).toEqual([
            'broker.close',
            'eventLog.close',
            'cache.close',
            'database.destroy',
        ]);
    });
});
