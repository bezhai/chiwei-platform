// 出站落库的真身发出去的到底是什么 SQL。
//
// 逻辑由 deliver.test.ts 用内存实现验；那份实现再怎么正确，也说明不了真身有没有
// 写错表、把 or-ignore 写丢、或者把两条 insert 漏在事务外面。开发机连不到数据库，
// 所以用不连库的录音 DataSource（见 ../recording-data-source.ts）看真实 SQL。

import { beforeEach, describe, expect, it } from 'bun:test';

import { LARK_SERVICE_ENTITIES } from '../../ormconfig';
import { recordingDataSource, type RecordedStatement } from '../recording-data-source';
import { postgresLarkOutboundTables } from './postgres-tables';
import type { LarkOutboundStore } from './tables';

interface Harness {
    store: LarkOutboundStore;
    recorded: RecordedStatement[];
    reply(rows: Array<Record<string, unknown>>): void;
    sqlOf(fragment: string): RecordedStatement;
    statements(): string[];
}

function harness(): Harness {
    const recorder = recordingDataSource([...LARK_SERVICE_ENTITIES]);
    return { ...recorder, store: postgresLarkOutboundTables(recorder.dataSource) };
}

let h: Harness;
beforeEach(() => {
    h = harness();
});

const ASSISTANT_ROW = {
    common_message_id: 'cm_1',
    channel: 'lark',
    common_conversation_id: 'cc_1',
    common_user_id: 'cu_bot',
    sender_display_name: '赤尾',
    role: 'assistant',
    content: [{ kind: 'text' as const, text: '在的' }],
    content_text: '在的',
    common_root_message_id: 'cm_root',
    common_reply_message_id: 'cm_trigger',
    scope: 'group',
    message_type: 'post',
    bot_name: 'chiwei',
    event_time: '1700000000000',
    response_id: 'sess-1',
    // 被动回复留空；主动发那一路由下面那条用例单独钉。
    agent_outbound_id: undefined,
};

const MAPPING_ROW = {
    om_id: 'om_1',
    common_message_id: 'cm_1',
    chat_id: 'oc_1',
    message_type: 'post',
};

describe('反查：公共层 id → 飞书坐标', () => {
    it('按 common_conversation_id 查 lark_base_chat_info，只取 chat_id', async () => {
        h.reply([
            {
                LarkBaseChatInfo_chat_id: 'oc_1',
                LarkBaseChatInfo_common_conversation_id: 'cc_1',
                LarkBaseChatInfo_chat_mode: 'group',
            },
        ]);

        expect(await h.store.chatIdOf('cc_1')).toBe('oc_1');

        const select = h.sqlOf('FROM "lark_base_chat_info"');
        expect(select.sql).toContain('"common_conversation_id" = $1');
        expect(select.params).toEqual(['cc_1']);
    });

    it('按 common_message_id 查 lark_message，只取 om_id', async () => {
        h.reply([{ LarkMessage_om_id: 'om_1', LarkMessage_common_message_id: 'cm_1' }]);

        expect(await h.store.omIdOf('cm_1')).toBe('om_1');

        const select = h.sqlOf('FROM "lark_message"');
        expect(select.sql).toContain('"common_message_id" = $1');
        expect(select.params).toEqual(['cm_1']);
    });

    it('反向：按 om_id 查回已有的 common_message_id', async () => {
        h.reply([{ LarkMessage_om_id: 'om_1', LarkMessage_common_message_id: 'cm_1' }]);

        expect(await h.store.commonMessageIdOf('om_1')).toBe('cm_1');

        const select = h.sqlOf('FROM "lark_message"');
        expect(select.sql).toContain('"om_id" = $1');
        expect(select.params).toEqual(['om_1']);
    });

    it('查不到就是 null，不编一个坐标出来', async () => {
        h.reply([]);
        expect(await h.store.chatIdOf('cc_missing')).toBeNull();
        h.reply([]);
        expect(await h.store.omIdOf('cm_missing')).toBeNull();
        h.reply([]);
        expect(await h.store.commonMessageIdOf('om_missing')).toBeNull();
    });
});

describe('写：assistant 行', () => {
    it('插 common_message 且冲突时静默跳过', async () => {
        await h.store.insertCommonMessage(ASSISTANT_ROW);

        const insert = h.sqlOf('INSERT INTO "common_message"');
        // 重投同一条出站消息时必须是 no-op，不是报错。
        expect(insert.sql).toContain('ON CONFLICT DO NOTHING');
        expect(insert.sql).toContain('"response_id"');
        expect(insert.params).toContain('cm_1');
        expect(insert.params).toContain('assistant');
        expect(insert.params).toContain('sess-1');
        // event_time 是 bigint 列，字符串原样落。
        expect(insert.params).toContain('1700000000000');
    });

    it('主动发那次开口的 id 真的进了 SQL —— 不是只留在行类型上', async () => {
        await h.store.insertCommonMessage({
            ...ASSISTANT_ROW,
            response_id: undefined,
            agent_outbound_id: '550e8400-e29b-41d4-a716-446655440000',
        });

        const insert = h.sqlOf('INSERT INTO "common_message"');
        expect(insert.sql).toContain('"agent_outbound_id"');
        expect(insert.params).toContain('550e8400-e29b-41d4-a716-446655440000');
    });

    it('content 作为 jsonb 整包落，不被拍平成字符串', async () => {
        await h.store.insertCommonMessage(ASSISTANT_ROW);

        const insert = h.sqlOf('INSERT INTO "common_message"');
        // TypeORM 把 jsonb 列的值序列化成一个字符串参数交给 pg 驱动，形状原样保留。
        expect(insert.params).toContain(JSON.stringify([{ kind: 'text', text: '在的' }]));
    });
});

describe('写：飞书映射', () => {
    it('插 lark_message，冲突时也静默跳过', async () => {
        await h.store.insertLarkMessage(MAPPING_ROW);

        const insert = h.sqlOf('INSERT INTO "lark_message"');
        // 出站的 om_id 真的会撞（平台没返回 id 时落的是合成键）。撞了回滚会把整条
        // 回复的落库全丢，而消息已经真的发出去了。
        expect(insert.sql).toContain('ON CONFLICT DO NOTHING');
        expect(insert.params).toContain('om_1');
        expect(insert.params).toContain('oc_1');
    });
});

describe('事务：两条 insert 同生共死', () => {
    it('atomically 里的写入包在 BEGIN / COMMIT 之间', async () => {
        await h.store.atomically(async (tables) => {
            await tables.insertCommonMessage(ASSISTANT_ROW);
            await tables.insertLarkMessage(MAPPING_ROW);
        });

        expect(h.statements()).toEqual([
            'begin',
            'INSERT INTO "common_message"("common_message_id",',
            'INSERT INTO "lark_message"("om_id",',
            'commit',
        ]);
    });

    it('run 抛错时回滚，且不 commit', async () => {
        await expect(
            h.store.atomically(async (tables) => {
                await tables.insertCommonMessage(ASSISTANT_ROW);
                throw new Error('mapping insert exploded');
            }),
        ).rejects.toThrow('mapping insert exploded');

        expect(h.statements()).toEqual([
            'begin',
            'INSERT INTO "common_message"("common_message_id",',
            'rollback',
        ]);
    });

    it('事务外的写入不带事务标记 —— 说明 atomically 真的换了 manager', async () => {
        await h.store.insertCommonMessage(ASSISTANT_ROW);
        expect(h.statements()).toEqual(['INSERT INTO "common_message"("common_message_id",']);
    });
});
