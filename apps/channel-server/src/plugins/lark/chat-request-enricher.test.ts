import { describe, it, expect, afterEach, beforeEach, mock } from 'bun:test';

import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';
import type { RuleMessage } from '@core/rules/rule-message';
import type { BotConfig } from '@entities/bot-config';

// 飞书侧 chat.request 富化：从 lark 私有 store 读 is_canary，并把已投影的
// common bot identity 收敛成 persona_id。agent-service 不碰 Lark app_id。

let botConfigs: Partial<BotConfig>[] = [];

mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: {
        getAllBotConfigs: () => botConfigs,
        getBotConfig: () => null,
    },
}));

mock.module('@middleware/context', () => ({
    context: {
        getBotName: () => undefined,
    },
}));

mock.module('ormconfig', () => ({
    default: {
        getRepository: () => ({
            findBy: async () => [],
        }),
    },
}));

const { enrichLarkChatRequest } = await import('./chat-request-enricher');

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'lark',
        botName: 'bot-x',
        commonUserId: 'U1',
        commonConversationId: 'C1',
        commonMessageId: 'GM',
        commonRootMessageId: undefined,
        isDirect: false,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 0,
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

beforeEach(() => {
    botConfigs = [];
});

afterEach(() => {
    larkContextStore.clear(rm());
});

describe('enrichLarkChatRequest', () => {
    it('reads is_canary and maps mentioned bot common_user_ids to persona_ids', () => {
        botConfigs = [
            {
                bot_name: 'bot-1',
                channel: 'lark',
                common_user_id: 'bot-common-1',
                persona_id: 'persona-1',
                credentials: {
                    app_id: 'app-1',
                    app_secret: 'sec',
                    encrypt_key: 'enc',
                    verification_token: 'vt',
                    robot_union_id: 'union-1',
                },
            },
            {
                bot_name: 'bot-2',
                channel: 'lark',
                common_user_id: 'bot-common-2',
                persona_id: 'persona-2',
                credentials: {
                    app_id: 'app-2',
                    app_secret: 'sec',
                    encrypt_key: 'enc',
                    verification_token: 'vt',
                    robot_union_id: 'union-2',
                },
            },
        ];
        const message = rm({
            mentionedUserIds: ['bot-common-1', 'human-common', 'bot-common-2'],
        });
        larkContextStore.put(message, {
            basicChatInfo: { permission_config: { is_canary: true } },
        } as unknown as Message);
        const e = enrichLarkChatRequest(message);
        expect(e.isCanary).toBe(true);
        expect(e.personaIds).toEqual(['persona-1', 'persona-2']);
    });

    it('defaults is_canary=false when permission_config missing', () => {
        const message = rm();
        larkContextStore.put(message, {
            basicChatInfo: undefined,
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
