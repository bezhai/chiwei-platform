import { describe, it, expect, afterEach } from 'bun:test';

import { enrichLarkChatRequest } from './chat-request-enricher';
import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';
import type { RuleMessage } from '@core/rules/rule-message';

// 飞书侧 chat.request 富化（B2）：从 lark 私有 store 取回飞书 Message 读
// is_canary / getBotAppIds，core 永远看不到飞书对象。非飞书 channel 中性默认。

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'lark',
        botName: 'bot-x',
        internalUserId: 'U1',
        internalChatId: 'C1',
        internalMessageId: 'GM',
        internalRootId: undefined,
        isDirect: false,
        addressedTargetIds: [],
        createTime: 0,
        clearText: () => '',
        text: () => '',
        withMentionText: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

afterEach(() => {
    larkContextStore.clear('GM');
});

describe('enrichLarkChatRequest', () => {
    it('reads is_canary + getBotAppIds from the lark Message in store', () => {
        larkContextStore.put('GM', {
            basicChatInfo: { permission_config: { is_canary: true } },
            getBotAppIds: () => ['app-1', 'app-2'],
        } as unknown as Message);
        const e = enrichLarkChatRequest(rm());
        expect(e.isCanary).toBe(true);
        expect(e.mentions).toEqual(['app-1', 'app-2']);
    });

    it('defaults is_canary=false when permission_config missing', () => {
        larkContextStore.put('GM', {
            basicChatInfo: undefined,
            getBotAppIds: () => [],
        } as unknown as Message);
        const e = enrichLarkChatRequest(rm());
        expect(e.isCanary).toBe(false);
        expect(e.mentions).toEqual([]);
    });

    it('non-lark channel: neutral default, never touches the store', () => {
        const e = enrichLarkChatRequest(rm({ channel: 'qq' }));
        expect(e.isCanary).toBe(false);
        expect(e.mentions).toEqual([]);
    });
});
