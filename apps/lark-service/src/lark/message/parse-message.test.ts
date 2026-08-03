import { describe, expect, it } from 'bun:test';

import { parseLarkMessage, splitMentionTokens } from './parse-message';
import type { LarkMessageEvent } from './wire';

function event(overrides: {
    messageType: string;
    content: string;
    message?: Partial<LarkMessageEvent['message']>;
    top?: Partial<LarkMessageEvent>;
}): LarkMessageEvent {
    return {
        app_id: 'cli_app',
        sender: { sender_type: 'user', sender_id: { union_id: 'on_u', open_id: 'ou_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: overrides.messageType,
            content: overrides.content,
            ...overrides.message,
        },
        ...overrides.top,
    };
}

describe('splitMentionTokens', () => {
    it('keeps a plain string as a single literal run', () => {
        expect(splitMentionTokens('hello world')).toEqual([{ kind: 'literal', text: 'hello world' }]);
    });

    it('cuts the text at every @_user_N token', () => {
        expect(splitMentionTokens('hi @_user_1 and @_user_2!')).toEqual([
            { kind: 'literal', text: 'hi ' },
            { kind: 'mention', token: '@_user_1' },
            { kind: 'literal', text: ' and ' },
            { kind: 'mention', token: '@_user_2' },
            { kind: 'literal', text: '!' },
        ]);
    });

    it('yields a lone mention run when the text is only a token', () => {
        expect(splitMentionTokens('@_user_1')).toEqual([{ kind: 'mention', token: '@_user_1' }]);
    });

    // 空文本必须留下一个空 literal run，否则两个投影都会把这条消息渲染成 0 个
    // 片段，而下游把 content 为空当作"解析失败"。
    it('keeps an empty text as one empty literal run', () => {
        expect(splitMentionTokens('')).toEqual([{ kind: 'literal', text: '' }]);
    });
});

describe('parseLarkMessage', () => {
    it('drops an event that carries no message id', () => {
        expect(parseLarkMessage({ sender: { sender_type: 'user' } } as LarkMessageEvent)).toBeNull();
        expect(
            parseLarkMessage(event({ messageType: 'text', content: '{}', message: { message_id: '' } })),
        ).toBeNull();
    });

    it('carries the event identity through untouched', () => {
        const parsed = parseLarkMessage(
            event({
                messageType: 'text',
                content: JSON.stringify({ text: 'hi' }),
                message: {
                    root_id: 'om_root',
                    parent_id: 'om_parent',
                    thread_id: 'omt_1',
                    chat_type: 'p2p',
                },
            }),
        );

        expect(parsed).not.toBeNull();
        expect(parsed!.messageId).toBe('om_1');
        expect(parsed!.rootId).toBe('om_root');
        expect(parsed!.parentId).toBe('om_parent');
        expect(parsed!.threadId).toBe('omt_1');
        expect(parsed!.chatId).toBe('oc_1');
        expect(parsed!.chatType).toBe('p2p');
        expect(parsed!.messageType).toBe('text');
        expect(parsed!.createTime).toBe('1700000000000');
        expect(parsed!.appId).toBe('cli_app');
        expect(parsed!.sender).toEqual({ unionId: 'on_u', openId: 'ou_u', userId: undefined });
    });

    it('keeps the mention records exactly as Lark delivered them', () => {
        const mentions = [{ key: '@_user_1', id: { union_id: 'on_a' }, name: 'A' }];
        const parsed = parseLarkMessage(
            event({
                messageType: 'text',
                content: JSON.stringify({ text: '@_user_1 hi' }),
                message: { mentions },
            }),
        );
        expect(parsed!.mentions).toEqual(mentions);
    });

    it('defaults the mention list to empty when Lark sends none', () => {
        const parsed = parseLarkMessage(event({ messageType: 'text', content: '{"text":"x"}' }));
        expect(parsed!.mentions).toEqual([]);
    });

    describe('segments', () => {
        const segmentsOf = (messageType: string, content: string) =>
            parseLarkMessage(event({ messageType, content }))!.segments;

        it('reads a text message into one text segment', () => {
            expect(segmentsOf('text', JSON.stringify({ text: 'hi @_user_1' }))).toEqual([
                {
                    kind: 'text',
                    runs: [
                        { kind: 'literal', text: 'hi ' },
                        { kind: 'mention', token: '@_user_1' },
                    ],
                },
            ]);
        });

        it('reads an image message', () => {
            expect(segmentsOf('image', JSON.stringify({ image_key: 'img_1' }))).toEqual([
                { kind: 'image', imageKey: 'img_1' },
            ]);
        });

        it('reads a sticker message', () => {
            expect(segmentsOf('sticker', JSON.stringify({ file_key: 'stk_1' }))).toEqual([
                { kind: 'sticker', fileKey: 'stk_1' },
            ]);
        });

        // post 是唯一会产出多个片段的类型：每个 text 节点是独立的一段，不与相邻
        // 节点合并 —— 合并会让通用契约那侧的 item 数量与现状不一致。
        it('reads a rich-text post node by node, one segment per node', () => {
            const post = {
                content: [
                    [
                        { tag: 'text', text: 'hello ' },
                        { tag: 'img', image_key: 'img_p' },
                    ],
                    [{ tag: 'text', text: 'second' }],
                ],
            };
            expect(segmentsOf('post', JSON.stringify(post))).toEqual([
                { kind: 'text', runs: [{ kind: 'literal', text: 'hello ' }] },
                { kind: 'image', imageKey: 'img_p' },
                { kind: 'text', runs: [{ kind: 'literal', text: 'second' }] },
            ]);
        });

        it('skips post nodes it cannot render and falls back when nothing is left', () => {
            const post = { content: [[{ tag: 'at', user_id: 'ou_x' }]] };
            expect(segmentsOf('post', JSON.stringify(post))).toEqual([
                { kind: 'text', runs: [{ kind: 'literal', text: '[富文本]' }] },
            ]);
        });

        it('reads a media message as a video with its poster and duration', () => {
            const media = {
                file_key: 'file_v',
                image_key: 'img_v',
                file_name: 'clip.mp4',
                duration: 30,
            };
            expect(segmentsOf('media', JSON.stringify(media))).toEqual([
                {
                    kind: 'video',
                    fileKey: 'file_v',
                    imageKey: 'img_v',
                    fileName: 'clip.mp4',
                    duration: 30,
                },
            ]);
        });

        it('reads a file message', () => {
            expect(
                segmentsOf('file', JSON.stringify({ file_key: 'file_1', file_name: 'a.pdf' })),
            ).toEqual([{ kind: 'file', fileKey: 'file_1', fileName: 'a.pdf' }]);
        });

        it('reads an audio message', () => {
            expect(segmentsOf('audio', JSON.stringify({ file_key: 'file_a', duration: 5 }))).toEqual([
                { kind: 'audio', fileKey: 'file_a', duration: 5 },
            ]);
        });

        // 认得出但不渲染的类型必须留下 originalType，否则"收到了没处理"不可观测。
        it.each([
            ['merge_forward', '[合并转发]'],
            ['share_chat', '[分享群名片]'],
            ['share_user', '[分享个人名片]'],
        ])('marks %s as unsupported with a readable placeholder', (type, placeholder) => {
            expect(segmentsOf(type, '{}')).toEqual([
                { kind: 'unsupported', placeholder, originalType: type },
            ]);
        });

        it('marks an unknown message type as unsupported, naming the type', () => {
            expect(segmentsOf('todo', '{}')).toEqual([
                { kind: 'unsupported', placeholder: '[todo]', originalType: 'todo' },
            ]);
        });
    });

    describe('malformed payloads', () => {
        // 飞书的 content 是一段 JSON 字符串。解析不了时每种类型各有自己的中文
        // 占位串，落库和给赤尾看的都是它 —— 换字面量等于改线上历史。
        it.each([
            ['text', '[文本]'],
            ['image', '[图片]'],
            ['sticker', '[表情包]'],
            ['post', '[富文本]'],
            ['media', '[视频]'],
            ['file', '[文件]'],
            ['audio', '[语音]'],
        ])('falls back to the %s placeholder when the payload is not JSON', (type, placeholder) => {
            const parsed = parseLarkMessage(event({ messageType: type, content: 'not json' }));
            expect(parsed!.segments).toEqual([
                { kind: 'text', runs: [{ kind: 'literal', text: placeholder }] },
            ]);
        });

        // 占位串本身绝不能再走 mention 替换：它不是用户写的文本。
        it('never mention-substitutes a fallback placeholder', () => {
            const parsed = parseLarkMessage(
                event({
                    messageType: 'text',
                    content: 'not json',
                    message: { mentions: [{ key: '@_user_1', id: { union_id: 'on_a' }, name: 'A' }] },
                }),
            );
            expect(parsed!.segments).toEqual([
                { kind: 'text', runs: [{ kind: 'literal', text: '[文本]' }] },
            ]);
        });
    });
});
