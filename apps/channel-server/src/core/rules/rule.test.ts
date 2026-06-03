import { describe, expect, it } from 'bun:test';
import { NeedNotRobotMention, NeedRobotMention } from './rule';
import type { RuleMessage } from './rule-message';

function msg(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'qq',
        botName: 'bot-x',
        commonUserId: 'U1',
        commonConversationId: 'C1',
        commonMessageId: 'M1',
        commonRootMessageId: undefined,
        isDirect: false,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 100,
        clearText: () => '',
        text: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

describe('NeedRobotMention', () => {
    it('direct messages always pass', () => {
        expect(NeedRobotMention(msg({ isDirect: true, mentionedUserIds: [] }))).toBe(true);
    });

    it('group messages pass only when common mention list contains current bot common user id', () => {
        expect(
            NeedRobotMention(
                msg({
                    botCommonUserId: 'BOT-U',
                    mentionedUserIds: ['OTHER-U', 'BOT-U'],
                }),
            ),
        ).toBe(true);
        expect(
            NeedRobotMention(
                msg({
                    botCommonUserId: 'BOT-U',
                    mentionedUserIds: ['OTHER-U'],
                }),
            ),
        ).toBe(false);
    });

    it('NeedNotRobotMention is the exact inverse', () => {
        const m = msg({ mentionedUserIds: ['OTHER-U'] });
        expect(NeedNotRobotMention(m)).toBe(true);
    });
});
