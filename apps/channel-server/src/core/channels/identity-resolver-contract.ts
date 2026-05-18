// 共享契约测试套件。T1 InMemoryIdentityResolver 把契约钉死，T5 DbIdentityResolver
// 必须满足同一套契约。把契约断言抽到这里，让两种实现复用同一组测试，避免 DB 版
// 偷偷放宽语义。这里只描述"对任意 IdentityResolver 实现都必须成立"的不变量，
// 不碰任何具体实现细节。
//
// 注意：本文件不替换 T1 的 identity-resolver.test.ts（那份保持原样、单独跑）。
// 这里是另起的可复用契约，给 DB 版用，并可选地让 InMemory 版复用做交叉验证。

import { describe, it, expect, beforeEach } from 'bun:test';

import {
    type IdentityResolver,
    type IdentityKind,
    IdentityNotFoundError,
} from './identity-resolver';

// 传入一个"每次产出全新空 resolver"的工厂，对它跑全套契约断言。
export function runIdentityResolverContract(
    label: string,
    makeResolver: () => IdentityResolver,
): void {
    describe(`IdentityResolver 契约 [${label}]`, () => {
        let r: IdentityResolver;
        beforeEach(() => {
            r = makeResolver();
        });

        const kinds: IdentityKind[] = ['user', 'chat', 'message'];

        it('resolve 幂等：同 (kind, channel, channelId) 多次解析返回同一全局 ID', async () => {
            for (const kind of kinds) {
                const a = await r.resolve(kind, 'lark', 'X');
                const b = await r.resolve(kind, 'lark', 'X');
                expect(a).toBe(b);
                expect(a).toBeString();
                expect(a.length).toBeGreaterThan(0);
            }
        });

        it('跨 channel 不串会话：相同 channelId 在不同 channel 下映射到不同全局 ID', async () => {
            for (const kind of kinds) {
                const lark = await r.resolve(kind, 'lark', 'SAME_ID');
                const qq = await r.resolve(kind, 'qq', 'SAME_ID');
                expect(lark).not.toBe(qq);
            }
        });

        it('同 channel 内不同 channelId 映射到不同全局 ID', async () => {
            const a = await r.resolve('user', 'lark', 'a');
            const b = await r.resolve('user', 'lark', 'b');
            expect(a).not.toBe(b);
        });

        it('三类身份是独立命名空间：user/chat/message 互不影响', async () => {
            const u = await r.resolve('user', 'lark', 'X');
            const c = await r.resolve('chat', 'lark', 'X');
            const m = await r.resolve('message', 'lark', 'X');
            expect(new Set([u, c, m]).size).toBe(3);
        });

        it('全局 ID 全局唯一：不同来源不会分配到同一个 ID', async () => {
            const ids = await Promise.all([
                r.resolve('user', 'lark', '1'),
                r.resolve('user', 'qq', '1'),
                r.resolve('chat', 'lark', '1'),
                r.resolve('user', 'lark', '2'),
            ]);
            expect(new Set(ids).size).toBe(ids.length);
        });

        it('channel 与 channelId 的边界不会因分隔符歧义而碰撞', async () => {
            const a = await r.resolve('user', 'a', 'b c');
            const b = await r.resolve('user', 'a b', 'c');
            expect(a).not.toBe(b);
        });

        it('toChannel 反查：能从全局 ID 还原出 (channel, channelId)', async () => {
            const internal = await r.resolve('chat', 'qq', 'group-42');
            const back = await r.toChannel('chat', internal);
            expect(back).toEqual({ channel: 'qq', channelId: 'group-42' });
        });

        it('toChannel 反查不到时必须明确报错，不能静默放过', async () => {
            await expect(
                r.toChannel('user', 'no-such-internal-id'),
            ).rejects.toBeInstanceOf(IdentityNotFoundError);
        });

        it('正反一致：每个 kind 多次 resolve 后 toChannel 都还原回原始 (channel, channelId)', async () => {
            const samples: Array<[IdentityKind, string, string]> = [
                ['user', 'lark', 'u1'],
                ['user', 'qq', 'u1'],
                ['chat', 'lark', 'c1'],
                ['message', 'qq', 'm1'],
            ];
            for (const [kind, channel, channelId] of samples) {
                const id = await r.resolve(kind, channel, channelId);
                expect(await r.toChannel(kind, id)).toEqual({ channel, channelId });
            }
        });

        it('并发 resolve 同一 (kind, channel, channelId) 不产生重复全局 ID', async () => {
            // spec 头号验收点之二：并发首次出现时唯一约束冲突必须收敛成同一个全局 ID
            const results = await Promise.all(
                Array.from({ length: 8 }, () => r.resolve('chat', 'qq', 'race-1')),
            );
            expect(new Set(results).size).toBe(1);
            // 落定后反查必须一致
            const back = await r.toChannel('chat', results[0]!);
            expect(back).toEqual({ channel: 'qq', channelId: 'race-1' });
        });
    });
}
