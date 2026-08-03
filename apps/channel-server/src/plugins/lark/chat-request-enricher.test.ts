import { describe, it, expect, afterAll, afterEach, beforeEach, mock } from 'bun:test';

import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';
import type { RuleMessage } from '@inner/shared/rules';
import type { BotConfig } from '@inner/shared/entities';

// 飞书侧 chat.request 富化：从 lark 私有 store 读 is_canary，并把已投影的
// common bot identity 收敛成 persona_id。agent-service 不碰 Lark app_id。

let botConfigs: Partial<BotConfig>[] = [];

// 富化路径只读 botDirectory（经由 bot-identity 的 common_user_id 反查）。
// bot-identity 里 ormconfig / @middleware/context 只在 loadLarkDisplayNames 和
// getCurrentLarkBot* 上用得到，本文件一条都走不到，所以不 mock 它们 —— 真身
// import 无副作用（ormconfig 只是构造 DataSource 并 bind，不连库）。
//
// botDirectory 这一个是真要控的：先抓真身铺底，跑完装回去，否则整模块替换会把
// BotDirectory 类从同进程后续文件眼里抹掉。
const realSharedBot = { ...(await import('@inner/shared/bot')) };

mock.module('@inner/shared/bot', () => ({
    ...realSharedBot,
    botDirectory: {
        getAllBotConfigs: () => botConfigs,
        getBotConfig: () => null,
    },
}));

afterAll(() => {
    mock.module('@inner/shared/bot', () => realSharedBot);
});

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
