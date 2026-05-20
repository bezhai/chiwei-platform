import { describe, it, expect, beforeEach } from 'bun:test';

import {
    type IdentityResolver,
    type IdentityKind,
    InMemoryIdentityResolver,
    IdentityNotFoundError,
} from './identity-resolver';

// IdentityResolver 是 channel 作用域身份模型的核心：把"channel 内 ID"翻译成
// channel 无关的全局内部 ID（进站正查），以及反向还原（出站反查）。
// 这里用内存实现验证契约本身；真正的 DB 绑定与飞书历史迁移是 T5。

describe('IdentityResolver 契约', () => {
    let r: IdentityResolver;
    beforeEach(() => {
        r = new InMemoryIdentityResolver();
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
        // spec 头号验收点：QQ 群 ID 与飞书 chat_id 是独立命名空间，绝不能撞
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
        // channelId 来自外部平台，不能假设它不含我们选的分隔符
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
        // 设计文档：反查不到要明确报错而不是静默放过
        await expect(r.toChannel('user', 'no-such-internal-id')).rejects.toBeInstanceOf(
            IdentityNotFoundError,
        );
    });
});
