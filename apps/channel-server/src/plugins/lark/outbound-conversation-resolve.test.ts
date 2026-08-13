import { afterAll, beforeEach, describe, expect, it, mock } from 'bun:test';

const larkChats = new Map<string, { chat_id: string; common_conversation_id: string }>();

// bun 的 mock.module 是**整模块替换 + 进程级全局**，且 mock.restore() 不撤销它。
// 先抓真身、只覆盖需要的导出，afterAll 再注回真身，否则同一个 bun test 进程里
// 后面加载的文件（含被测生产代码）会看到残缺模块。ormconfig 只导出 default
// （TypeORM DataSource）；import 真身只做 bindDataSource 存引用，不建连接。
const realOrmconfig = { ...(await import('ormconfig')) };
mock.module('ormconfig', () => ({
    ...realOrmconfig,
    default: {
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'LarkBaseChatInfo') {
                return {
                    findOne: mock(
                        async ({
                            where,
                        }: {
                            where: { common_conversation_id: string };
                        }) => larkChats.get(where.common_conversation_id) ?? null,
                    ),
                };
            }
            throw new Error(`unexpected repository: ${entity.name}`);
        },
    },
}));

const { resolveLarkConversationRef } = await import('./outbound-reverse-resolve');

// 主动发（is_proactive）没有来源消息，只有真实的 common_conversation_id（p2p 会话）。
// resolveLarkConversationRef 只做会话反查：common_conversation_id → 飞书裸 chat_id，
// 绝不碰 lark_message 表（那才是被动回复反查源消息时做的事）。
// 查不到 fail-loud，绝不静默把主动发的消息送到错地方。

describe('resolveLarkConversationRef（会话独立反查，主动发用）', () => {
    beforeEach(() => {
        larkChats.clear();
    });

    it('common_conversation_id -> 飞书裸 chat_id（不碰 lark_message）', async () => {
        larkChats.set('018f-p2p', {
            common_conversation_id: '018f-p2p',
            chat_id: 'oc_real_p2p',
        });

        const ref = await resolveLarkConversationRef('018f-p2p');
        expect(ref.channelId).toBe('oc_real_p2p');
    });

    it('查不到会话 -> fail-loud', async () => {
        await expect(resolveLarkConversationRef('018f-missing')).rejects.toThrow(
            /common_conversation_id=018f-missing/,
        );
    });
});

afterAll(() => {
    mock.module('ormconfig', () => realOrmconfig);
});
