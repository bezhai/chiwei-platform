import { describe, it, expect } from 'bun:test';

import { reverseResolveOutbound } from './outbound-reverse-resolve';
import { InMemoryIdentityResolver } from '@core/channels/identity-resolver';

// 出站（chat-response-worker）：收到带全局 internal_*_id 的回复 →
// IdentityResolver.toChannel 反查回飞书裸 ID → 飞书富文本 native 出站。
// reverseResolveOutbound 加 channel==='lark' 边界断言：防 T6 后误把非飞书 ID
// 喂飞书发送器。反查不到 = fail-loud（toChannel 抛 IdentityNotFoundError），
// 绝不静默发错。这是飞书出站适配器的职责，故住在 plugins/lark。

describe('reverseResolveOutbound', () => {
    it('reverse-resolves global ids back to lark channel ids', async () => {
        const r = new InMemoryIdentityResolver();
        const gm = await r.resolve('message', 'lark', 'lark-m');
        const gc = await r.resolve('chat', 'lark', 'lark-c');
        const gr = await r.resolve('message', 'lark', 'lark-root');

        const out = await reverseResolveOutbound({
            resolver: r,
            messageGlobalId: gm,
            chatGlobalId: gc,
            rootGlobalId: gr,
        });
        expect(out.channelMessageId).toBe('lark-m');
        expect(out.channelChatId).toBe('lark-c');
        expect(out.channelRootId).toBe('lark-root');
    });

    it('asserts channel===lark — non-lark global id is rejected (boundary, no wrong-send)', async () => {
        const r = new InMemoryIdentityResolver();
        const gmQQ = await r.resolve('message', 'qq', 'qq-m');
        const gcQQ = await r.resolve('chat', 'qq', 'qq-c');
        await expect(
            reverseResolveOutbound({
                resolver: r,
                messageGlobalId: gmQQ,
                chatGlobalId: gcQQ,
                rootGlobalId: undefined,
            }),
        ).rejects.toThrow(/lark/i);
    });

    it('reverse-resolve miss -> throws (fail-loud, never silent wrong-send)', async () => {
        const r = new InMemoryIdentityResolver();
        await expect(
            reverseResolveOutbound({
                resolver: r,
                messageGlobalId: 'never-seen',
                chatGlobalId: 'never-seen-2',
                rootGlobalId: undefined,
            }),
        ).rejects.toThrow();
    });

    it('no root global id -> channelRootId undefined', async () => {
        const r = new InMemoryIdentityResolver();
        const gm = await r.resolve('message', 'lark', 'm');
        const gc = await r.resolve('chat', 'lark', 'c');
        const out = await reverseResolveOutbound({
            resolver: r,
            messageGlobalId: gm,
            chatGlobalId: gc,
            rootGlobalId: undefined,
        });
        expect(out.channelRootId).toBeUndefined();
    });
});
