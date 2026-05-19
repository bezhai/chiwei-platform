import { describe, it, expect, mock, beforeEach } from 'bun:test';
import type { ChatMessage } from 'types/chat';

// 身份全局化后 conversation_messages.user_id 从飞书 union_id 变成全局
// internal_user_id，读取端不再 JOIN lark_user 取显示名，改读
// conversation_messages.username 冗余列。写入端（storeMessage）必须把
// 发送者显示名一并落到 username 列，否则读取端读到的永远是空。
//
// 本测试钉死写入契约：storeMessage 把 ChatMessage.username 透传进
// INSERT 的 values() payload 的 username 字段。全程不连真实 DB ——
// AppDataSource / context / rabbitmq 全 mock，断言落在捕获的 values()
// 入参上（与 src 现有 mock.module 测试同风格）。

let capturedValues: Record<string, unknown> | undefined;

const insertExecute = mock(async () => ({ identifiers: [{ message_id: 'm1' }] }));
const valuesMock = mock((v: Record<string, unknown>) => {
    capturedValues = v;
    return { orIgnore: () => ({ execute: insertExecute }) };
});
const intoMock = mock(() => ({ values: valuesMock }));
const insertMock = mock(() => ({ into: intoMock }));

mock.module('ormconfig', () => ({
    default: {
        createQueryBuilder: () => ({ insert: insertMock }),
    },
}));

mock.module('@entities/conversation-message', () => ({
    ConversationMessage: class {},
}));

mock.module('@middleware/context', () => ({
    context: {
        getBotName: () => 'chiwei',
        getLane: () => undefined,
    },
}));

mock.module('@integrations/rabbitmq', () => ({
    rabbitmqClient: { publish: mock(async () => undefined) },
    VECTORIZE: 'vectorize',
}));

const { storeMessage } = await import('./memory');

describe('storeMessage 写入 username 冗余列', () => {
    beforeEach(() => {
        capturedValues = undefined;
    });

    it('把发送者显示名透传进 INSERT values().username', async () => {
        const msg: ChatMessage = {
            user_id: 'internal_user_42',
            content: 'hello',
            role: 'user',
            message_id: 'm1',
            chat_id: 'c1',
            chat_type: 'group',
            create_time: '1700000000000',
            username: 'Alice',
        };

        await storeMessage(msg);

        expect(capturedValues).toBeDefined();
        expect(capturedValues!.username).toBe('Alice');
        // 不破坏原有字段
        expect(capturedValues!.user_id).toBe('internal_user_42');
    });

    it('没有 username 时落 null（不抛、不写脏占位）', async () => {
        const msg: ChatMessage = {
            user_id: 'internal_user_99',
            content: 'hi',
            role: 'assistant',
            message_id: 'm2',
            chat_id: 'c1',
            chat_type: 'p2p',
            create_time: '1700000000001',
        };

        await storeMessage(msg);

        expect(capturedValues).toBeDefined();
        expect(capturedValues!.username ?? null).toBeNull();
    });
});
