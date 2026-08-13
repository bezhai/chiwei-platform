import { afterEach, beforeEach, describe, expect, it } from 'bun:test';
import type { DataSource } from 'typeorm';

import { context } from '../middleware/context';
import { CommonAgentResponse } from '../entities/common-agent-response';
import { bindDataSource, resetBoundDataSource } from '../persistence/data-source';
import { makeTextReply } from './chat-request';
import type { PendingChatTrigger } from './engine';
import type { RuleMessage } from './rule-message';

// 入站重排：makeTextReply 不再自己 publish / 取去重锁 / 落 common_agent_response
// pending 行。它在 runRules 阶段只做纯预备工作：buildChatRequestPayload + 构造
// pending 行落库闭包 savePending；通过 ctx.registerPendingChatTrigger 登记
// payload + lane + dedupeKey + savePending，由渠道侧接线点在入站消息写入成功、
// 抢到去重锁后才调 savePending() 并 publish（多 bot 同群只有抢锁 bot 写 pending
// 行，未抢锁 bot 不留孤儿 pending 行）。本测试钉死：
//
//   makeTextReply 调用后 —— 不 publish、**不立即 save pending 行**，而是
//   registerPendingChatTrigger 被调用一次，payload 全局 ID 正确，savePending
//   是个尚未执行的闭包；仅当显式调用 captured.savePending() 时行才落库。
//
// 「不 publish」这里不靠 mock 断言，靠真身兜底：进程内没有连过 MQ，一旦
// makeTextReply 真去 publish，rabbitmq client 会在 getChannel() 直接抛
// 「channel not available」把用例炸掉。比桩更强，也不会像 mock.module 那样
// 泄漏到同一轮的其他测试文件。

const saved: Partial<CommonAgentResponse>[] = [];
const created: Partial<CommonAgentResponse>[] = [];

function fakeDataSource(): DataSource {
    return {
        getRepository(entity: unknown) {
            if (entity !== CommonAgentResponse) {
                throw new Error(`unexpected repository request: ${String(entity)}`);
            }
            return {
                create(values: Partial<CommonAgentResponse>) {
                    created.push(values);
                    return values;
                },
                async save(row: Partial<CommonAgentResponse>) {
                    saved.push(row);
                },
            };
        },
    } as unknown as DataSource;
}

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'channel-x',
        botName: 'bot-q',
        commonUserId: 'GU',
        commonConversationId: 'GC',
        commonMessageId: 'GM',
        commonRootMessageId: 'GR',
        isDirect: true,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 1,
        clearText: () => 'hi',
        text: () => 'hi',
        withoutEmojiText: () => 'hi',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

function inRequestContext<T>(cb: () => Promise<T>): Promise<T> {
    return context.run({ traceId: 't', botName: 'bot-q', lane: 'ppe-x' }, cb);
}

beforeEach(() => {
    saved.length = 0;
    created.length = 0;
    resetBoundDataSource();
    bindDataSource(fakeDataSource());
});

afterEach(() => {
    resetBoundDataSource();
});

describe('makeTextReply registers pending ChatTrigger instead of publishing', () => {
    it('登记待发意图，携带全局 id 与 lane；pending 行此刻不落库', async () => {
        let captured: PendingChatTrigger | undefined;
        await inRequestContext(async () =>
            makeTextReply(rm(), {
                registerPendingChatTrigger: (p) => {
                    captured = p;
                },
            }),
        );

        expect(captured).toBeDefined();
        expect(captured!.payload.message_id).toBe('GM');
        expect(captured!.payload.chat_id).toBe('GC');
        expect(captured!.payload.user_id).toBe('GU');
        expect(captured!.payload.root_id).toBe('GR');
        expect(captured!.payload.channel).toBe('channel-x');
        expect(captured!.payload.is_p2p).toBe(true);
        expect(captured!.payload.bot_name).toBe('bot-q');
        expect(captured!.lane).toBe('ppe-x');
        // 去重锁键后移到接线点，但 key 口径必须跟旧实现一致
        expect(captured!.dedupeKey).toBe('make_reply:GM');

        // pending 行 save 后移 —— makeTextReply 内不得落库，只登记一个尚未
        // 执行的闭包。
        expect(saved).toHaveLength(0);
        expect(typeof captured!.savePending).toBe('function');
    });

    it('显式调 savePending() 才真正落 pending 行，字段与 session 对齐', async () => {
        let captured: PendingChatTrigger | undefined;
        await inRequestContext(async () =>
            makeTextReply(rm(), {
                registerPendingChatTrigger: (p) => {
                    captured = p;
                },
            }),
        );

        await captured!.savePending();

        expect(saved).toHaveLength(1);
        expect(created).toHaveLength(1);
        expect(created[0]).toMatchObject({
            session_id: captured!.payload.session_id,
            trigger_common_message_id: 'GM',
            common_conversation_id: 'GC',
            bot_name: 'bot-q',
            status: 'pending',
        });
    });

    it('落库失败只记日志、不抛 —— pending 行是观测便利，不是发 MQ 的前置条件', async () => {
        resetBoundDataSource();
        bindDataSource({
            getRepository: () => ({
                create: (v: unknown) => v,
                save: async () => {
                    throw new Error('pg down');
                },
            }),
        } as unknown as DataSource);

        let captured: PendingChatTrigger | undefined;
        await inRequestContext(async () =>
            makeTextReply(rm(), {
                registerPendingChatTrigger: (p) => {
                    captured = p;
                },
            }),
        );
        await expect(captured!.savePending()).resolves.toBeUndefined();
    });

    it('无 ctx 传入时不 throw（接线点一定传 ctx，单元健壮性钉死）', async () => {
        await inRequestContext(async () => makeTextReply(rm()));
        expect(saved).toHaveLength(0);
    });
});
