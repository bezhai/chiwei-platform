import { afterEach, beforeEach, describe, expect, it } from 'bun:test';
import type { DataSource, FindManyOptions } from 'typeorm';

import { BotConfig } from '../entities/bot-config';
import { CommonUser } from '../entities/common-user';
import { bindDataSource, resetBoundDataSource } from '../persistence/data-source';
import { BotDirectory } from './bot-directory';

// bot 身份目录本身是渠道无关的：它只知道 bot_config 有一个 channel 列，不知道
// 具体取值是什么。但**按 channel 过滤加载**是拆服务的硬约束 —— 一个服务的进程内
// 不该出现别的渠道的 credentials。所以过滤必须发生在查询条件里（行压根不取回来），
// 而不是取回全表再在内存里挑：后者等于凭据已经进了进程，只是没被读而已。

interface FindCall {
    entity: unknown;
    options: FindManyOptions<Record<string, unknown>>;
}

interface UpsertCall {
    entity: unknown;
    values: Record<string, unknown>;
    conflictPaths: string[];
}

interface UpdateCall {
    entity: unknown;
    criteria: Record<string, unknown>;
    values: Record<string, unknown>;
}

const findCalls: FindCall[] = [];
const upsertCalls: UpsertCall[] = [];
const updateCalls: UpdateCall[] = [];

let rows: Partial<BotConfig>[] = [];

function fakeDataSource(): DataSource {
    return {
        getRepository(entity: unknown) {
            return {
                async find(options: FindManyOptions<Record<string, unknown>>) {
                    findCalls.push({ entity, options });
                    const where = (options.where ?? {}) as Record<string, unknown>;
                    return rows.filter((r) => matchesWhere(r, where));
                },
                async upsert(values: Record<string, unknown>, conflictPaths: string[]) {
                    upsertCalls.push({ entity, values, conflictPaths });
                },
                async update(criteria: Record<string, unknown>, values: Record<string, unknown>) {
                    updateCalls.push({ entity, criteria, values });
                },
            };
        },
    } as unknown as DataSource;
}

// 只支持相等与 In(...)：够钉死"过滤进了查询条件"这件事，不必复刻 typeorm。
function matchesWhere(row: Record<string, unknown>, where: Record<string, unknown>): boolean {
    return Object.entries(where).every(([key, expected]) => {
        if (isInOperator(expected)) return expected.value.includes(row[key] as never);
        return row[key] === expected;
    });
}

function isInOperator(v: unknown): v is { _type: string; value: unknown[] } {
    return (
        typeof v === 'object' &&
        v !== null &&
        (v as { _type?: string })._type === 'in' &&
        Array.isArray((v as { value?: unknown }).value)
    );
}

function bot(over: Partial<BotConfig> = {}): Partial<BotConfig> {
    return {
        bot_name: 'bot-a',
        channel: 'channel-x',
        init_type: 'http',
        is_active: true,
        is_dev: false,
        bot_role: 'persona',
        common_user_id: 'user-a',
        ...over,
    };
}

beforeEach(() => {
    findCalls.length = 0;
    upsertCalls.length = 0;
    updateCalls.length = 0;
    rows = [];
    resetBoundDataSource();
    bindDataSource(fakeDataSource());
});

afterEach(() => {
    resetBoundDataSource();
});

describe('BotDirectory.load: 只取启用的 bot', () => {
    it('查询条件带 is_active=true', async () => {
        rows = [bot()];
        await new BotDirectory().load();
        expect(findCalls).toHaveLength(1);
        expect(findCalls[0]!.entity).toBe(BotConfig);
        expect((findCalls[0]!.options.where as Record<string, unknown>).is_active).toBe(true);
    });

    it('按 bot_name 索引，未知 bot 返回 null', async () => {
        rows = [bot({ bot_name: 'a' }), bot({ bot_name: 'b' })];
        const dir = new BotDirectory();
        await dir.load();
        expect(dir.getBotConfig('a')?.bot_name).toBe('a');
        expect(dir.getBotConfig('nope')).toBeNull();
        expect(dir.getAllBotConfigs().map((b) => b.bot_name)).toEqual(['a', 'b']);
    });

    it('重复 load 不重复查库（已加载即幂等）', async () => {
        rows = [bot()];
        const dir = new BotDirectory();
        await dir.load();
        await dir.load();
        expect(findCalls).toHaveLength(1);
    });
});

describe('BotDirectory.load: 按 channel 过滤（拆服务硬约束）', () => {
    it('不传 channels 时不加 channel 条件 —— 全渠道加载', async () => {
        rows = [
            bot({ bot_name: 'a', channel: 'channel-x' }),
            bot({ bot_name: 'b', channel: 'channel-y' }),
        ];
        const dir = new BotDirectory();
        await dir.load();
        expect((findCalls[0]!.options.where as Record<string, unknown>).channel).toBeUndefined();
        expect(dir.getAllBotConfigs().map((b) => b.bot_name)).toEqual(['a', 'b']);
    });

    it('传 channels 时过滤下推到查询条件 —— 别的渠道的行压根不取回来', async () => {
        rows = [
            bot({ bot_name: 'a', channel: 'channel-x' }),
            bot({ bot_name: 'b', channel: 'channel-y' }),
        ];
        const dir = new BotDirectory();
        await dir.load({ channels: ['channel-x'] });

        const where = findCalls[0]!.options.where as Record<string, unknown>;
        expect(isInOperator(where.channel)).toBe(true);
        expect((where.channel as { value: unknown[] }).value).toEqual(['channel-x']);
        expect(dir.getAllBotConfigs().map((b) => b.bot_name)).toEqual(['a']);
    });

    it('被过滤掉的渠道的 bot 完全不可见（凭据不进本进程）', async () => {
        rows = [
            bot({ bot_name: 'a', channel: 'channel-x', credentials: { secret: 'x-secret' } }),
            bot({ bot_name: 'b', channel: 'channel-y', credentials: { secret: 'y-secret' } }),
        ];
        const dir = new BotDirectory();
        await dir.load({ channels: ['channel-x'] });

        expect(dir.getBotConfig('b')).toBeNull();
        const seen = JSON.stringify(dir.getAllBotConfigs());
        expect(seen).not.toContain('y-secret');
    });

    it('channels 传空数组 fail-closed —— 静默加载零个 bot 等于服务变砖', async () => {
        const dir = new BotDirectory();
        await expect(dir.load({ channels: [] })).rejects.toThrow(/channels/i);
    });

    // 幂等 + 范围收窄 = 隔离被架空：先无参 load() 把所有渠道的 bot（含凭据）读进
    // 进程，再 load({channels:[...]}) 因为"已加载"直接返回，别的渠道的凭据仍然
    // 留在内存里。静默返回等于让调用方以为自己拿到了隔离，实际没有 —— 必须炸。
    it('先全渠道 load 再收窄范围：fail-loud，不静默沿用已加载的全渠道结果', async () => {
        rows = [
            bot({ bot_name: 'a', channel: 'channel-x', credentials: { secret: 'x-secret' } }),
            bot({ bot_name: 'b', channel: 'channel-y', credentials: { secret: 'y-secret' } }),
        ];
        const dir = new BotDirectory();
        await dir.load();
        expect(dir.getBotConfig('b')).not.toBeNull();

        await expect(dir.load({ channels: ['channel-x'] })).rejects.toThrow(/already loaded/i);
    });

    it('反向：先收窄再无参 load 也 fail-loud（范围扩大同样是范围不一致）', async () => {
        rows = [bot({ bot_name: 'a', channel: 'channel-x' })];
        const dir = new BotDirectory();
        await dir.load({ channels: ['channel-x'] });
        await expect(dir.load()).rejects.toThrow(/already loaded/i);
    });

    it('换一组 channels 也 fail-loud', async () => {
        rows = [bot({ bot_name: 'a', channel: 'channel-x' })];
        const dir = new BotDirectory();
        await dir.load({ channels: ['channel-x'] });
        await expect(dir.load({ channels: ['channel-y'] })).rejects.toThrow(/already loaded/i);
    });

    it('同一范围重复 load 仍幂等（启动链路多处调用不该炸）', async () => {
        rows = [bot({ bot_name: 'a', channel: 'channel-x' })];
        const dir = new BotDirectory();
        await dir.load({ channels: ['channel-x'] });
        await dir.load({ channels: ['channel-x'] });
        // 顺序不同但集合相同也算同一范围
        await dir.load({ channels: ['channel-x'] });
        expect(findCalls).toHaveLength(1);
    });

    it('范围不一致时报错必须点名两次的范围，便于定位是谁先加载的', async () => {
        rows = [bot({ bot_name: 'a', channel: 'channel-x' })];
        const dir = new BotDirectory();
        await dir.load({ channels: ['channel-x'] });
        await expect(dir.load({ channels: ['channel-y'] })).rejects.toThrow(
            /channel-x[\s\S]*channel-y|channel-y[\s\S]*channel-x/,
        );
    });
});

describe('BotDirectory: bot 的 common 身份', () => {
    it('已有 common_user_id：upsert common_user，不回写 bot_config', async () => {
        rows = [bot({ bot_name: 'a', common_user_id: 'u-1' })];
        const dir = new BotDirectory();
        await dir.load();

        expect(upsertCalls).toHaveLength(1);
        expect(upsertCalls[0]!.entity).toBe(CommonUser);
        expect(upsertCalls[0]!.values).toMatchObject({
            common_user_id: 'u-1',
            channel: 'channel-x',
            display_name: 'a',
        });
        expect(upsertCalls[0]!.conflictPaths).toEqual(['common_user_id']);
        expect(updateCalls).toHaveLength(0);
        expect(dir.getBotCommonUserId('a')).toBe('u-1');
    });

    it('缺 common_user_id：新分配一个、回写 bot_config、内存里立刻可见', async () => {
        rows = [bot({ bot_name: 'a', common_user_id: undefined })];
        const dir = new BotDirectory();
        await dir.load();

        expect(upsertCalls).toHaveLength(1);
        const assigned = upsertCalls[0]!.values.common_user_id as string;
        expect(assigned).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-/i);

        expect(updateCalls).toHaveLength(1);
        expect(updateCalls[0]!.entity).toBe(BotConfig);
        expect(updateCalls[0]!.criteria).toEqual({ bot_name: 'a' });
        expect(updateCalls[0]!.values).toEqual({ common_user_id: assigned });

        expect(dir.getBotCommonUserId('a')).toBe(assigned);
    });

    it('未知 bot 取身份直接抛错，不返回空串让下游拿脏 id 去写库', async () => {
        const dir = new BotDirectory();
        await dir.load();
        expect(() => dir.getBotCommonUserId('ghost')).toThrow(/not found/i);
    });
});

describe('BotDirectory.getBotsByInitType', () => {
    it('按 init_type 过滤', async () => {
        rows = [
            bot({ bot_name: 'h', init_type: 'http' }),
            bot({ bot_name: 'w', init_type: 'websocket' }),
        ];
        const dir = new BotDirectory();
        await dir.load();
        expect(dir.getBotsByInitType('http').map((b) => b.bot_name)).toEqual(['h']);
        expect(dir.getBotsByInitType('websocket').map((b) => b.bot_name)).toEqual(['w']);
    });

    it('onlyCurrentEnv=true 时按 IS_DEV 匹配 is_dev', async () => {
        rows = [
            bot({ bot_name: 'prod-bot', is_dev: false }),
            bot({ bot_name: 'dev-bot', is_dev: true }),
        ];
        const dir = new BotDirectory();
        await dir.load();

        const saved = process.env.IS_DEV;
        try {
            process.env.IS_DEV = 'true';
            expect(dir.getBotsByInitType('http', true).map((b) => b.bot_name)).toEqual(['dev-bot']);
            process.env.IS_DEV = 'false';
            expect(dir.getBotsByInitType('http', true).map((b) => b.bot_name)).toEqual([
                'prod-bot',
            ]);
            // onlyCurrentEnv=false 时不看环境
            expect(dir.getBotsByInitType('http').map((b) => b.bot_name)).toEqual([
                'prod-bot',
                'dev-bot',
            ]);
        } finally {
            if (saved === undefined) delete process.env.IS_DEV;
            else process.env.IS_DEV = saved;
        }
    });
});
