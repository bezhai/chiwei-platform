// 把用户刚说的那条消息拼回一串带 `<at>` 标签的字。
//
// `<at>` 标签丢了 union_id，复读出来就是一个光秃秃的 "@"，读的人不知道 @ 的是谁。
// 这串字再往下变成富文本（表情、分行）是 ../outbound/post-content.ts 的事，用例在那边。

import { describe, expect, it } from 'bun:test';

import type { LarkContentPart } from '../message/lark-content';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import { larkAtTaggedText } from './echo';

describe('正文 → 带 <at> 标签的文本', () => {
    it('被 @ 的人写成飞书认的标签，其余片段原样拼回去', () => {
        const parts: LarkContentPart[] = [
            { type: 'text', value: '早上好 ' },
            { type: 'mention', value: '张三', meta: { channel_user_id: 'on_zhang' } },
            { type: 'text', value: ' 今天也来啦' },
        ];

        expect(larkAtTaggedText(parts)).toBe('早上好 <at user_id="on_zhang"></at> 今天也来啦');
    });

    // 拿不到 union_id 的 mention（飞书偶尔只给 open_id）退回文字形式。发一个
    // `user_id=""` 的标签飞书会拒收整条消息。
    it('没有 union_id 的 @ 退回 "@显示名"', () => {
        const parts: LarkContentPart[] = [
            { type: 'mention', value: '李四', meta: {} },
            { type: 'text', value: ' 在吗' },
        ];

        expect(larkAtTaggedText(parts)).toBe('@李四 在吗');
    });

    it('meta 里是空串也退回 "@显示名"', () => {
        const parts: LarkContentPart[] = [
            { type: 'mention', value: '李四', meta: { channel_user_id: '' } },
        ];

        expect(larkAtTaggedText(parts)).toBe('@李四');
    });

    // 端到端走一遍真的解析：mention 的 union_id 到底有没有落进 meta.channel_user_id，
    // 手搓的片段说明不了。
    it('从真的飞书事件解析出来的正文也接得上', () => {
        const bots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };
        const event: LarkMessageEvent = {
            app_id: 'cli_x',
            sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
            message: {
                message_id: 'om_1',
                chat_id: 'oc_1',
                chat_type: 'group',
                create_time: '1700000000000',
                message_type: 'text',
                content: '{"text":"@_user_1 早"}',
                mentions: [{ key: '@_user_1', id: { union_id: 'on_zhang' }, name: '张三' }],
            },
        };

        const reading = readLarkMessageEvent(event, bots)!;

        expect(larkAtTaggedText(reading.content)).toBe('<at user_id="on_zhang"></at> 早');
    });
});
