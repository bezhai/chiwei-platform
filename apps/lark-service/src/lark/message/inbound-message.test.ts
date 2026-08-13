import { describe, expect, it } from 'bun:test';

import { inboundMessageOf } from './inbound-message';
import { resolveLarkMentions, type LarkBotLookup } from './mentions';
import { parseLarkMessage } from './parse-message';
import type { LarkMention, LarkMessageBody, LarkMessageEvent } from './wire';

const noBots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };

function project(
    message: Partial<LarkMessageBody> & { message_type: string; content: string },
    top: Partial<LarkMessageEvent> = {},
    bots: LarkBotLookup = noBots,
) {
    const event: LarkMessageEvent = {
        app_id: 'cli_app',
        sender: { sender_type: 'user', sender_id: { union_id: 'on_u', open_id: 'ou_u' } },
        ...top,
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            ...message,
        },
    };
    const parsed = parseLarkMessage(event)!;
    return inboundMessageOf(parsed, resolveLarkMentions(parsed.mentions, bots));
}

describe('inboundMessageOf', () => {
    it('speaks the shared channel contract, not Lark vocabulary', () => {
        const inbound = project({ message_type: 'text', content: JSON.stringify({ text: 'hi' }) });
        expect(inbound.channel).toBe('lark');
        expect(inbound.channel_message_id).toBe('om_1');
        expect(inbound.channel_chat_id).toBe('oc_1');
        expect(inbound.received_at).toBe(1700000000000);
    });

    // 这里的 bot_name 装的其实是飞书 app_id。看着别扭，但下游（寻址、投影）就是
    // 按这个口径读的，本次拆分不动它。
    it('carries the Lark app id in bot_name', () => {
        expect(project({ message_type: 'text', content: '{"text":"x"}' }).bot_name).toBe('cli_app');
        expect(
            project({ message_type: 'text', content: '{"text":"x"}' }, { app_id: undefined }).bot_name,
        ).toBe('');
    });

    // 发送者用 open_id 而不是 union_id：这条 id 只在本渠道内有意义，投影层负责
    // 把它换成全局身份。
    it('identifies the sender by open id, and says so when Lark sent none', () => {
        expect(project({ message_type: 'text', content: '{"text":"x"}' }).channel_user_id).toBe(
            'ou_u',
        );
        expect(
            project(
                { message_type: 'text', content: '{"text":"x"}' },
                { sender: { sender_type: 'user', sender_id: { union_id: 'on_u' } } },
            ).channel_user_id,
        ).toBe('unknown_sender');
    });

    it('maps a private chat to direct and everything else to group', () => {
        expect(
            project({ message_type: 'text', content: '{"text":"x"}', chat_type: 'p2p' })
                .conversation_scope,
        ).toBe('direct');
        expect(
            project({ message_type: 'text', content: '{"text":"x"}', chat_type: 'topic' })
                .conversation_scope,
        ).toBe('group');
    });

    // 飞书出站是"回复触发的那条消息、留在话题串里"，所以入站消息自己就是回复锚点。
    it('anchors the reply on the message itself and stays in the thread', () => {
        expect(project({ message_type: 'text', content: '{"text":"x"}' }).thread_ref).toEqual({
            selfChannelMessageId: 'om_1',
            inThread: true,
        });
    });

    it('adds the reply and root anchors only when Lark sent them', () => {
        expect(
            project({
                message_type: 'text',
                content: '{"text":"x"}',
                parent_id: 'om_p',
                root_id: 'om_r',
            }).thread_ref,
        ).toEqual({
            selfChannelMessageId: 'om_1',
            inThread: true,
            replyToChannelMessageId: 'om_p',
            rootChannelMessageId: 'om_r',
        });
    });

    it('offers every mentioned union id as an addressing hint', () => {
        const mentions: LarkMention[] = [
            { key: '@_user_1', id: { union_id: 'on_a' }, name: 'A' },
            { key: '@_user_2', id: { union_id: 'on_b' }, name: 'B' },
        ];
        expect(
            project({ message_type: 'text', content: '{"text":"x"}', mentions }).addressing_hints,
        ).toEqual([{ targetId: 'on_a' }, { targetId: 'on_b' }]);
    });

    describe('content', () => {
        // 通用契约里没有 mention 这种片段，@ 必须内联回文本；而且**一段源文本
        // 只产出一个 item** —— 拆成三个会让下游看到的片段数量与拆分前不一致。
        it('inlines mentions back into one text item', () => {
            const mentions: LarkMention[] = [
                { key: '@_user_1', id: { union_id: 'on_a' }, name: '张三' },
            ];
            expect(
                project({
                    message_type: 'text',
                    content: JSON.stringify({ text: 'hi @_user_1 !' }),
                    mentions,
                }).content,
            ).toEqual([{ kind: 'text', text: 'hi @张三 !' }]);
        });

        it('leaves an unmatched token in place', () => {
            expect(
                project({ message_type: 'text', content: JSON.stringify({ text: 'a @_user_7 b' }) })
                    .content,
            ).toEqual([{ kind: 'text', text: 'a @_user_7 b' }]);
        });

        it('keeps one item per rich-text node', () => {
            const post = {
                content: [
                    [
                        { tag: 'text', text: 'hello ' },
                        { tag: 'img', image_key: 'img_p' },
                    ],
                    [{ tag: 'text', text: 'second' }],
                ],
            };
            expect(project({ message_type: 'post', content: JSON.stringify(post) }).content).toEqual([
                { kind: 'text', text: 'hello ' },
                { kind: 'image', key: 'img_p' },
                { kind: 'text', text: 'second' },
            ]);
        });

        it('maps an image and a sticker to their contract kinds', () => {
            expect(
                project({ message_type: 'image', content: JSON.stringify({ image_key: 'i' }) })
                    .content,
            ).toEqual([{ kind: 'image', key: 'i' }]);
            expect(
                project({ message_type: 'sticker', content: JSON.stringify({ file_key: 's' }) })
                    .content,
            ).toEqual([{ kind: 'sticker', key: 's' }]);
        });

        // 通用契约没有"视频"这一类，视频和文件都是可下载附件；lark_type 留在 meta
        // 里，让本渠道自己还能分辨。
        it('carries a video as a downloadable file, tagged with its Lark type', () => {
            expect(
                project({
                    message_type: 'media',
                    content: JSON.stringify({
                        file_key: 'f_v',
                        image_key: 'i_v',
                        file_name: 'clip.mp4',
                        duration: 30,
                    }),
                }).content,
            ).toEqual([
                {
                    kind: 'file',
                    key: 'f_v',
                    meta: {
                        image_key: 'i_v',
                        file_name: 'clip.mp4',
                        duration: 30,
                        lark_type: 'media',
                    },
                },
            ]);
        });

        it('tags a real file with its Lark type too', () => {
            expect(
                project({
                    message_type: 'file',
                    content: JSON.stringify({ file_key: 'f', file_name: 'a.pdf' }),
                }).content,
            ).toEqual([
                { kind: 'file', key: 'f', meta: { file_name: 'a.pdf', lark_type: 'file' } },
            ]);
        });

        it('maps audio to the audio kind with its duration', () => {
            expect(
                project({
                    message_type: 'audio',
                    content: JSON.stringify({ file_key: 'f_a', duration: 5 }),
                }).content,
            ).toEqual([{ kind: 'audio', key: 'f_a', meta: { duration: 5 } }]);
        });

        it('reports an unrendered type as unsupported rather than dropping it', () => {
            expect(project({ message_type: 'share_chat', content: '{}' }).content).toEqual([
                { kind: 'unsupported', text: '[分享群名片]', meta: { original_type: 'share_chat' } },
            ]);
        });
    });

    it('reads a non-numeric create time as zero rather than NaN', () => {
        expect(
            project({ message_type: 'text', content: '{"text":"x"}', create_time: 'nope' })
                .received_at,
        ).toBe(0);
    });
});
