import { describe, it, expect, afterEach } from 'bun:test';

import { enrichLarkChatRequest } from './chat-request-enricher';
import { larkContextStore } from './lark-context-store';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import type { Message } from '@core/models/message';
import type { RuleMessage } from '@core/rules/rule-message';

// 飞书侧 chat.request 富化：从 lark 私有 store 读 is_canary / getBotAppIds，
// 并在插件内把 Lark app_id 收敛成 persona_id。agent-service 不碰 Lark app_id。

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'lark',
        botName: 'bot-x',
        commonUserId: 'U1',
        commonConversationId: 'C1',
        commonMessageId: 'GM',
        commonRootMessageId: undefined,
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
    larkContextStore.clear(rm());
    multiBotManager.getBotConfigByAppId = originalGetBotConfigByAppId;
});

const originalGetBotConfigByAppId = multiBotManager.getBotConfigByAppId;

describe('enrichLarkChatRequest', () => {
    it('reads is_canary and maps mentioned bot app_ids to persona_ids', () => {
        multiBotManager.getBotConfigByAppId = ((appId: string) => {
            const map: Record<string, { persona_id: string }> = {
                'app-1': { persona_id: 'persona-1' },
                'app-2': { persona_id: 'persona-2' },
            };
            return map[appId] ?? null;
        }) as typeof multiBotManager.getBotConfigByAppId;
        const message = rm();
        larkContextStore.put(message, {
            basicChatInfo: { permission_config: { is_canary: true } },
            getBotAppIds: () => ['app-1', 'app-2'],
        } as unknown as Message);
        const e = enrichLarkChatRequest(message);
        expect(e.isCanary).toBe(true);
        expect(e.personaIds).toEqual(['persona-1', 'persona-2']);
    });

    it('defaults is_canary=false when permission_config missing', () => {
        const message = rm();
        larkContextStore.put(message, {
            basicChatInfo: undefined,
            getBotAppIds: () => [],
        } as unknown as Message);
        const e = enrichLarkChatRequest(message);
        expect(e.isCanary).toBe(false);
        expect(e.personaIds).toEqual([]);
    });

    it('non-lark channel: neutral default, never touches the store', () => {
        const e = enrichLarkChatRequest(rm({ channel: 'qq' }));
        expect(e.isCanary).toBe(false);
        expect(e.personaIds).toEqual([]);
    });
});
