import { describe, it, expect, mock, afterEach } from 'bun:test';

// 飞书侧 utility-redirect 引导提示（B2 从 engine.ts 搬进 plugins/lark）。
// 它从 lark 私有 store 按 commonMessageId 取回飞书 Message，对原始
// messageId 发飞书回复（reply_in_thread=true）。engine 只调注入的 responder。

const replyMessageMock = mock(() => {});
mock.module('@lark/basic/message', () => ({
    replyMessage: replyMessageMock,
}));

import { sendLarkUtilityRedirect } from './utility-redirect';
import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';
import type { RuleMessage } from '@core/rules/rule-message';

function rm(commonMessageId: string): RuleMessage {
    return {
        channel: 'lark',
        botName: 'bot-x',
        commonUserId: 'U1',
        commonConversationId: 'C1',
        commonMessageId,
        commonRootMessageId: undefined,
        isDirect: false,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 0,
        clearText: () => '',
        text: () => '',
        withMentionText: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
    };
}

afterEach(() => {
    replyMessageMock.mockClear();
    larkContextStore.clear(rm('GM'));
});

describe('sendLarkUtilityRedirect', () => {
    it('replies to the raw lark messageId fetched from the plugin store', () => {
        const message = rm('GM');
        larkContextStore.put(message, { messageId: 'lark-raw-1' } as unknown as Message);
        sendLarkUtilityRedirect(message);
        expect(replyMessageMock).toHaveBeenCalledTimes(1);
        const [target, text, inThread] = replyMessageMock.mock.calls[0] as unknown as [
            string,
            string,
            boolean,
        ];
        expect(target).toBe('lark-raw-1');
        expect(text).toContain('工具类功能已迁移至「赤尾工具人」');
        expect(inThread).toBe(true);
    });

    it('fail-loud when lark Message absent from store (no silent skip)', () => {
        expect(() => sendLarkUtilityRedirect(rm('MISSING'))).toThrow(/lark/i);
    });
});
