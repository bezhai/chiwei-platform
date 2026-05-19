import { describe, it, expect, mock, beforeEach } from 'bun:test';
import type { ChatMessage } from 'types/chat';

// 死分支修复（fail-loud）契约钉死：
//
// 背景：handlers.ts 入站对 storeMessage 已有 try/catch fail-loud（存库失败
// 则不 savePending 不 publish），但 memory.ts storeMessage 的 catch 把所有
// DB 错误吞掉、返回 void、永不 rethrow —— 对真实 PG 故障是死分支：PG 真挂
// 时 storeMessage 不抛、handlers 照常 publish，下游 agent-service
// find_message_content 读空回 "未找到记录"。
//
// 本测试钉死真实 memory.ts storeMessage 的语义：
//   ① 真实 DB 故障（execute() reject：连接失败/超时/非预期错误）
//      → storeMessage 必须 rethrow（不得吞）
//   ② ON CONFLICT DO NOTHING 去重（execute() 正常 resolve、identifiers 空）
//      → storeMessage 不抛、视为成功（行已存在、可回查），不发向量化
//   ③ 正常插入成功（identifiers 非空）→ 不抛、发向量化
//
// 全程不连真实 DB —— AppDataSource / context / rabbitmq 全 mock，与
// memory.username.test.ts 同风格。

let executeBehavior: 'inserted' | 'conflict_skip' | 'throw' = 'inserted';

const insertExecute = mock(async () => {
    if (executeBehavior === 'throw') {
        // 真实 PG 故障：TypeORM execute() reject（连接拒绝/超时/未预期错误）
        throw new Error('connect ECONNREFUSED 10.0.0.1:5432');
    }
    if (executeBehavior === 'conflict_skip') {
        // ON CONFLICT DO NOTHING：PG 在 SQL 层吃掉冲突，execute() 正常
        // resolve，但没有行被插入 → identifiers 为空。
        return { identifiers: [] as Array<{ message_id: string }> };
    }
    return { identifiers: [{ message_id: 'm1' }] };
});
const valuesMock = mock(() => ({
    orIgnore: () => ({ execute: insertExecute }),
}));
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

const publishMock = mock(async () => undefined);
mock.module('@integrations/rabbitmq', () => ({
    rabbitmqClient: { publish: publishMock },
    VECTORIZE: 'vectorize',
}));

const { storeMessage } = await import('./memory');

const baseMsg: ChatMessage = {
    user_id: 'internal_user_42',
    content: 'hello',
    role: 'user',
    message_id: 'm1',
    chat_id: 'c1',
    chat_type: 'group',
    create_time: '1700000000000',
};

describe('storeMessage fail-loud：真实 DB 故障必须 rethrow', () => {
    beforeEach(() => {
        executeBehavior = 'inserted';
        publishMock.mockClear();
    });

    it('① 真实 DB 故障（execute reject）→ storeMessage rethrow（不吞）', async () => {
        executeBehavior = 'throw';
        await expect(storeMessage({ ...baseMsg })).rejects.toThrow(
            /ECONNREFUSED/,
        );
        // 真实故障未写入 → 绝不发向量化
        expect(publishMock).not.toHaveBeenCalled();
    });

    it('② ON CONFLICT 去重（execute resolve、identifiers 空）→ 不抛、视为成功、不发向量化', async () => {
        executeBehavior = 'conflict_skip';
        // 不得抛：行已存在（别的 bot 先插），下游可回查，是正常去重不是故障
        await storeMessage({ ...baseMsg });
        expect(publishMock).not.toHaveBeenCalled();
    });

    it('③ 正常插入成功 → 不抛、发向量化一次', async () => {
        executeBehavior = 'inserted';
        await storeMessage({ ...baseMsg });
        expect(publishMock).toHaveBeenCalledTimes(1);
    });
});
