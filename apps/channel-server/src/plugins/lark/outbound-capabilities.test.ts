import { describe, it, expect } from 'bun:test';

import { createLarkOutboundCapabilities } from './outbound-capabilities';
import type { LarkOutboundDeps } from './outbound-capabilities';
import type { ContentItem, ThreadRef } from '@core/channels/contracts';
import type { PostContent } from 'types/content-types';

// B3：plugins/lark 的 OutboundCapabilities 实现。把现状 chat-response-worker
// inline 的飞书富文本出站管线（image registry 解析 + 上传飞书、@用户名 mention
// 解析、markdown→PostContent、send/reply/delete）收进端口，飞书 SDK 调用只此一处。
//
// 端口契约：sendText(conv, content, ctx) / reply(thread, content, ctx) /
// recall(msg)。content 是平台无关 ContentItem[]（worker 传 AI 原始 markdown 文本）；
// ctx 必填，携带飞书渲染所需的外部引用（image registry 全局 id、群 mention 用的 larkChatId）。

// ---- 可注入 deps 的 spy 工厂（不碰真实飞书 SDK / redis / DB）----
function makeDeps(over: Partial<LarkOutboundDeps> = {}): {
    deps: LarkOutboundDeps;
    calls: {
        sent: Array<{ chatId: string; content: PostContent }>;
        replied: Array<{ messageId: string; content: PostContent; replyInThread: boolean }>;
        deleted: string[];
        uploaded: number;
        registryQueried: string[];
        mentionResolved: Array<{ content: string; chatId: string }>;
    };
} {
    const calls = {
        sent: [] as Array<{ chatId: string; content: PostContent }>,
        replied: [] as Array<{ messageId: string; content: PostContent; replyInThread: boolean }>,
        deleted: [] as string[],
        uploaded: 0,
        registryQueried: [] as string[],
        mentionResolved: [] as Array<{ content: string; chatId: string }>,
    };
    const deps: LarkOutboundDeps = {
        async send(chatId, content) {
            calls.sent.push({ chatId, content });
            return { message_id: `sent_${chatId}` };
        },
        async reply(messageId, content, replyInThread) {
            calls.replied.push({ messageId, content, replyInThread });
            return { message_id: `replied_${messageId}` };
        },
        async deleteMessage(messageId) {
            calls.deleted.push(messageId);
        },
        async uploadImage() {
            calls.uploaded++;
            return { image_key: 'img_key_uploaded' };
        },
        async getImageRegistry(key) {
            calls.registryQueried.push(key);
            return { '1.png': 'https://tos.example/1.png' };
        },
        async resolveMentionsForGroup(content, chatId) {
            calls.mentionResolved.push({ content, chatId });
            return content.replaceAll('@小明', '<at user_id="u_xiaoming">小明</at>');
        },
        async fetchImage() {
            return Buffer.from('fake-image-bytes');
        },
        ...over,
    };
    return { deps, calls };
}

const text = (s: string): ContentItem[] => [{ kind: 'text', text: s }];

describe('lark OutboundCapabilities', () => {
    describe('reply', () => {
        it('回复触发消息：markdown→post 发给 reply、reply_in_thread 透传、返回新消息 ref', async () => {
            const { deps, calls } = makeDeps();
            const cap = createLarkOutboundCapabilities(deps);

            const thread: ThreadRef = { selfChannelMessageId: 'om_trigger', inThread: true };
            const ref = await cap.reply(thread, text('你好世界'), {
                imageRegistryId: 'global_msg_1',
                groupConversationId: 'oc_chat',
            });

            expect(calls.replied.length).toBe(1);
            expect(calls.replied[0].messageId).toBe('om_trigger');
            expect(calls.replied[0].replyInThread).toBe(true);
            // markdown→PostContent：纯文本 → 一个 md 节点
            expect(calls.replied[0].content.content[0][0]).toEqual({ tag: 'md', text: '你好世界' });
            expect(calls.sent.length).toBe(0);
            // 返回的 MessageRef 带回飞书新消息 id
            expect(ref.channelId).toBe('replied_om_trigger');
        });

        it('群聊回复：@用户名经 resolveMentionsForGroup 翻成 <at>，用 ctx.groupConversationId', async () => {
            const { deps, calls } = makeDeps();
            const cap = createLarkOutboundCapabilities(deps);

            await cap.reply({ selfChannelMessageId: 'om_t', inThread: true }, text('喂 @小明 在吗'), {
                imageRegistryId: 'g1',
                groupConversationId: 'oc_group',
                resolveMentions: true,
            });

            expect(calls.mentionResolved).toEqual([{ content: '喂 @小明 在吗', chatId: 'oc_group' }]);
            expect(calls.replied[0].content.content[0]).toEqual([
                { tag: 'md', text: '喂 ' },
                { tag: 'at', user_id: 'u_xiaoming' },
                { tag: 'md', text: ' 在吗' },
            ]);
        });

        it('p2p 回复（resolveMentions=false）：不调 resolveMentionsForGroup', async () => {
            const { deps, calls } = makeDeps();
            const cap = createLarkOutboundCapabilities(deps);

            await cap.reply({ selfChannelMessageId: 'om_t', inThread: true }, text('私聊 @小明'), {
                imageRegistryId: 'g1',
                groupConversationId: 'oc_p2p',
                resolveMentions: false,
            });

            expect(calls.mentionResolved.length).toBe(0);
        });

        it('@N.png 图片引用：查 registry(用 ctx.imageRegistryId) → 下载 → uploadImage → 替换 image_key', async () => {
            const { deps, calls } = makeDeps();
            const cap = createLarkOutboundCapabilities(deps);

            await cap.reply({ selfChannelMessageId: 'om_t', inThread: true }, text('看图 ![pic](1.png)'), {
                imageRegistryId: 'global_msg_for_registry',
                groupConversationId: 'oc_chat',
            });

            // registry 必须用全局 imageRegistryId 查，绝不能用飞书裸 id
            expect(calls.registryQueried).toEqual(['image_registry:global_msg_for_registry']);
            expect(calls.uploaded).toBe(1);
            // markdown→post 渲染出 img 节点，image_key 是上传后的 key
            const nodes = calls.replied[0].content.content.flat();
            const img = nodes.find((n) => (n as { tag: string }).tag === 'img') as { image_key: string };
            expect(img).toBeDefined();
            expect(img.image_key).toBe('img_key_uploaded');
        });

        it('无回复锚点（thread 无 self/replyTo/root）fail-loud，绝不静默发错', async () => {
            const { deps } = makeDeps();
            const cap = createLarkOutboundCapabilities(deps);
            await expect(
                cap.reply({ inThread: true } as ThreadRef, text('x'), { imageRegistryId: 'g', groupConversationId: 'c' }),
            ).rejects.toThrow();
        });
    });

    describe('sendText', () => {
        it('新发消息：markdown→post 发给 conv.channelId、返回新消息 ref', async () => {
            const { deps, calls } = makeDeps();
            const cap = createLarkOutboundCapabilities(deps);

            const ref = await cap.sendText({ channelId: 'oc_chat' }, text('新消息'), {
                imageRegistryId: 'g2',
                groupConversationId: 'oc_chat',
            });

            expect(calls.sent.length).toBe(1);
            expect(calls.sent[0].chatId).toBe('oc_chat');
            expect(calls.sent[0].content.content[0][0]).toEqual({ tag: 'md', text: '新消息' });
            expect(calls.replied.length).toBe(0);
            expect(ref.channelId).toBe('sent_oc_chat');
        });
    });

    describe('recall', () => {
        it('撤回：调飞书 deleteMessage 传入裸 message id', async () => {
            const { deps, calls } = makeDeps();
            const cap = createLarkOutboundCapabilities(deps);

            await cap.recall!({ channelId: 'om_to_delete' });

            expect(calls.deleted).toEqual(['om_to_delete']);
        });
    });
});
