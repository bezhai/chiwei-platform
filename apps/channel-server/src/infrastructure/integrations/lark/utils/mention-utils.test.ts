import { describe, expect, it, mock } from 'bun:test';
import { ContentType } from '@core/models/message-content';

mock.module('@plugins/lark/bot-identity', () => ({
    getLarkBotConfigByAppId: () => null,
    getLarkBotConfigByUnionId: () => null,
    getLarkDisplayNameByAppId: () => null,
    larkCredentials: () => {
        throw new Error('not used');
    },
}));

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
