import { describe, it, expect } from 'bun:test';
import AppDataSource from './ormconfig';
import { QqUserOpenId, QqMessage, QqGroupChatInfo } from './infrastructure/dal/entities';

// QQ 入站首条消息会 getRepository(QqUserOpenId)。这 3 张 QQ entity 被 import 进
// ormconfig 但漏加进 entities 数组 → 运行期 "No metadata found"、QQ 链路首投影崩。
// 这里钉死它们必须在 DataSource 注册的 entities 列表里。
describe('ormconfig QQ entity registration', () => {
    const entities = AppDataSource.options.entities as Function[];

    it('registers QqUserOpenId', () => {
        expect(entities).toContain(QqUserOpenId);
    });

    it('registers QqMessage', () => {
        expect(entities).toContain(QqMessage);
    });

    it('registers QqGroupChatInfo', () => {
        expect(entities).toContain(QqGroupChatInfo);
    });
});
