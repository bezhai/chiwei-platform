import { describe, expect, it } from 'bun:test';
import { ContentType } from '@core/models/message-content';

// 这里刻意不 mock @plugins/lark/bot-identity：本文件只测 applyMentionTokens，它是
// 纯字符串处理，运行时一次都不碰 bot-identity（只有 addMentions 会）。真身 import
// 也只是建模块级 Map、绑 DataSource 引用，不连库。而 bun 的 mock.module 是进程级
// 全局替换，多余的 mock 会把 bot-identity 顶掉污染后面的文件。
const REAL_MENTION_UTILS = new URL('./mention-utils.ts', import.meta.url).href;
const { MentionUtils } = await import(REAL_MENTION_UTILS);

describe('MentionUtils', () => {
    it('converts Lark @_user_N tokens into neutral mention content items', () => {
        const mentions = [
            { id: 'on_alice', displayName: 'Alice' },
            { id: 'on_bot', displayName: '赤尾', botCommonUserId: 'bot-common' },
        ];

        const items = MentionUtils.applyMentionTokens(
            [{ type: ContentType.Text, value: '@_user_1 hi @_user_2' }],
            mentions,
        );

        expect(items).toEqual([
            {
                type: ContentType.Mention,
                value: 'Alice',
                meta: { channel_user_id: 'on_alice', bot_common_user_id: undefined },
            },
            { type: ContentType.Text, value: ' hi ' },
            {
                type: ContentType.Mention,
                value: '赤尾',
                meta: { channel_user_id: 'on_bot', bot_common_user_id: 'bot-common' },
            },
        ]);
    });
});
