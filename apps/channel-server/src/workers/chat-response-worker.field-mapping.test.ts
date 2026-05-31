import { describe, it, expect } from 'bun:test';

import { reverseResolveOutbound } from '../plugins/lark/outbound-reverse-resolve';
import { InMemoryIdentityResolver } from '../core/channels/identity-resolver';

// Bug 2 字段语义契约（钉死 publish ↔ consume 双侧字段口径）：
//
// chat-response-worker 把 chat.response payload 里的 ``message_id`` /
// ``chat_id`` / ``root_id`` 当全局 internal_*_id ULID 喂给
// reverseResolveOutbound。如果 publish 端（agent-service ChatResponseSegment）
// 在这三个字段里塞了飞书裸 om_*/oc_* 之类的 channel-native id，
// reverseResolveOutbound 必抛 IdentityNotFoundError —— 整段回复炸（prod 已遇到
// "no message identity mapping for internal id \"om_x100b6fec...\""）。
//
// 本测试钉死：
//   (a) ChatResponsePayload 三个 id 字段都按全局 ULID 解释；
//   (b) 给个飞书裸 om_*/oc_* 会让 reverseResolveOutbound fail-loud，永远不静默
//       发到错地方 —— 即"publish 端塞错了字段值"的 contract violation 必被
//       消费方在边界炸出。

interface PublishedChatResponse {
    session_id: string;
    message_id: string;
    chat_id: string;
    root_id?: string;
}

// 模拟 chat-response-worker handleChatResponse 里那段从 payload destruct
// 后喂给 reverseResolveOutbound 的关键映射；不引入 worker 全量依赖。
async function consumeFieldMapping(
    payload: PublishedChatResponse,
    resolver: InMemoryIdentityResolver,
): Promise<{
    channelMessageId: string;
    channelChatId: string;
    channelRootId: string | undefined;
}> {
    return reverseResolveOutbound({
        resolver,
        messageGlobalId: payload.message_id,
        chatGlobalId: payload.chat_id,
        rootGlobalId: payload.root_id || undefined,
    });
}

describe('chat-response 字段映射契约：payload 三个 id 字段必须是全局 ULID', () => {
    it('publish 端正确填全局 ULID -> consume 端反查回 lark 裸 id（happy path）', async () => {
        const r = new InMemoryIdentityResolver();
        const globalMsg = await r.resolve('message', 'lark', 'om_real_msg');
        const globalChat = await r.resolve('chat', 'lark', 'oc_real_chat');
        const globalRoot = await r.resolve('message', 'lark', 'om_real_root');

        const out = await consumeFieldMapping(
            {
                session_id: 's1',
                message_id: globalMsg,
                chat_id: globalChat,
                root_id: globalRoot,
            },
            r,
        );
        expect(out.channelMessageId).toBe('om_real_msg');
        expect(out.channelChatId).toBe('oc_real_chat');
        expect(out.channelRootId).toBe('om_real_root');
    });

    it('publish 端误把飞书裸 om_* 塞进 message_id -> reverseResolveOutbound 必抛 IdentityNotFoundError (fail-loud)', async () => {
        const r = new InMemoryIdentityResolver();
        const globalChat = await r.resolve('chat', 'lark', 'oc_x');

        // 复现 prod 报错形态：message_id 字段值是 lark om_x... 而非全局 ULID
        await expect(
            consumeFieldMapping(
                {
                    session_id: 's2',
                    message_id: 'om_x100b6fecc8a838a4c3643c45e7a98db',
                    chat_id: globalChat,
                    root_id: undefined,
                },
                r,
            ),
        ).rejects.toThrow(/no message identity mapping/i);
    });

    it('publish 端误把飞书裸 om_* 塞进 root_id -> reverseResolveOutbound 必抛 IdentityNotFoundError (fail-loud)', async () => {
        // 这是 Bug 2 真实场景：agent-service submit_proactive_chat 把 lark 裸
        // om_* 当全局 ULID 放进 ChatTrigger.root_id 后流到 ChatResponseSegment
        // .root_id —— consume 端必须在边界炸，绝不静默发错。
        const r = new InMemoryIdentityResolver();
        const globalMsg = await r.resolve('message', 'lark', 'om_msg');
        const globalChat = await r.resolve('chat', 'lark', 'oc_chat');

        await expect(
            consumeFieldMapping(
                {
                    session_id: 's3',
                    message_id: globalMsg,
                    chat_id: globalChat,
                    root_id: 'om_x100b6fecc8a838a4c3643c45e7a98db',
                },
                r,
            ),
        ).rejects.toThrow(/no message identity mapping/i);
    });
});
