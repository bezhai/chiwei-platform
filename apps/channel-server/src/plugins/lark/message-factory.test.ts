import { beforeEach, describe, expect, it, mock } from 'bun:test';
import { ContentType } from '@core/models/message-content';

let baseChatInfo: any = null;
let groupChatInfo: any = null;
let senderInfo: any = null;

mock.module('@infrastructure/dal/repositories/repositories', () => ({
    BaseChatInfoRepository: {
        findOne: mock(async () => baseChatInfo),
    },
    GroupChatInfoRepository: {
        findOne: mock(async () => groupChatInfo),
    },
    UserRepository: {
        findOne: mock(async () => senderInfo),
    },
}));

const { createLarkMessageFromEvent, createLarkMessageFromHistory } = await import(
    './message-factory'
);

function receiveEvent(overrides: Record<string, unknown> = {}) {
    const { message: messageOverrides, ...rest } = overrides;
    return {
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'p2p',
            message_type: 'text',
            content: '{"text":"hello"}',
            create_time: '1710000000000',
            ...((messageOverrides as Record<string, unknown> | undefined) ?? {}),
        },
        sender: {
            sender_id: {
                union_id: 'ou_sender',
                open_id: 'open_sender',
            },
        },
        ...rest,
    } as any;
}

describe('lark message factory', () => {
    beforeEach(() => {
        baseChatInfo = null;
        groupChatInfo = null;
        senderInfo = null;
    });

    it('builds Message metadata for p2p events inside the lark plugin', async () => {
        baseChatInfo = { chat_id: 'oc_1', chat_mode: 'p2p' };
        senderInfo = { union_id: 'ou_sender', name: 'sender' };

        const message = await createLarkMessageFromEvent(receiveEvent(), {
            items: [{ type: ContentType.Text, value: 'hello' }],
            mentions: [],
        });

        expect(message.messageId).toBe('om_1');
        expect(message.isP2P()).toBe(true);
        expect(message.rootId).toBe('om_1');
        expect(message.basicChatInfo).toBe(baseChatInfo);
        expect(message.senderInfo).toBe(senderInfo);
        expect(message.senderOpenId).toBe('open_sender');
    });

    it('uses group chat info and permission when building group events', async () => {
        const groupBase: any = { chat_id: 'oc_group', chat_mode: 'group' };
        groupChatInfo = {
            chat_id: 'oc_group',
            baseChatInfo: groupBase,
            download_has_permission_setting: 'not_anyone',
        };

        const message = await createLarkMessageFromEvent(
            receiveEvent({
                message: {
                    chat_id: 'oc_group',
                    chat_type: 'group',
                    root_id: 'om_root',
                },
            }),
            {
                items: [{ type: ContentType.Image, value: 'img_1' }],
                mentions: [],
            },
        );

        expect(message.isP2P()).toBe(false);
        expect(message.rootId).toBe('om_root');
        expect(message.basicChatInfo).toBe(groupBase);
        expect(message.groupChatInfo).toBe(groupChatInfo);
        expect(message.allowDownloadResource()).toBe(false);
    });

    it('builds Message content from lark history messages inside the plugin', () => {
        const message = createLarkMessageFromHistory({
            message_id: 'om_history',
            chat_id: 'oc_history',
            sender: { id: 'ou_history', id_type: 'user_id' },
            body: { content: '{"text":"from history"}' },
            mentions: [{ id: 'ou_mentioned' }],
            create_time: '1710000000001',
        } as any);

        expect(message.messageId).toBe('om_history');
        expect(message.text()).toBe('from history');
        expect(message.getMentionedUsers()).toEqual(['ou_mentioned']);
        expect(message.isRobotMessage).toBe(false);
    });
});
