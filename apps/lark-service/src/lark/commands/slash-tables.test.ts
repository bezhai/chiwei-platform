// 斜杠子指令要读写、而投影那份端口没有覆盖的两张表：`user_blacklist` 和
// `common_message`。
//
// 开发机连不到库，所以用不连库的录音 DataSource 看 TypeORM 真的生成的 SQL
// （见 ../recording-data-source.ts）。这里要钉的都是"写错了不报错"的东西：
//
//   * 拉黑写的是哪一列（`union_id`），删的是哪一行；
//   * `/session` 读 common_message 时**必须带上 role**，否则拿别人的消息也能查出
//     session id 来。

import { beforeEach, describe, expect, it } from 'bun:test';

import { LARK_SERVICE_ENTITIES } from '../../ormconfig';
import { recordingDataSource, type RecordedStatement } from '../recording-data-source';
import { postgresAgentSessions, postgresBlocklist } from './slash-tables';
import type { LarkAgentSessions, LarkBlocklist } from './slash-tables';

interface Harness {
    blocklist: LarkBlocklist;
    sessions: LarkAgentSessions;
    recorded: RecordedStatement[];
    reply(rows: Array<Record<string, unknown>>): void;
    sqlOf(fragment: string): RecordedStatement;
    statements(): string[];
}

function harness(): Harness {
    const recorder = recordingDataSource([...LARK_SERVICE_ENTITIES]);
    return {
        ...recorder,
        blocklist: postgresBlocklist(recorder.dataSource),
        sessions: postgresAgentSessions(recorder.dataSource),
    };
}

let h: Harness;
beforeEach(() => {
    h = harness();
});

describe('黑名单', () => {
    it('查一个人在不在名单里', async () => {
        h.reply([{ UserBlacklist_union_id: 'on_a' }]);

        expect(await h.blocklist.isBlocked('on_a')).toBe(true);

        const select = h.sqlOf('FROM "user_blacklist"');
        expect(select.params).toEqual(['on_a']);
    });

    it('查不到就是没被拉黑', async () => {
        h.reply([]);
        expect(await h.blocklist.isBlocked('on_a')).toBe(false);
    });

    // 写进去的是 union_id 和"谁拉的"。列名写错的后果是这一行永远匹配不上读取方，
    // 而拉黑那一步照样回"拉黑成功"。
    it('拉黑写 union_id 与操作人', async () => {
        await h.blocklist.block('on_a', 'on_admin');

        const insert = h.sqlOf('INSERT INTO "user_blacklist"');
        expect(insert.sql).toContain('"union_id"');
        expect(insert.sql).toContain('"blocked_by"');
        expect(insert.params).toEqual(expect.arrayContaining(['on_a', 'on_admin']));
    });

    it('解除拉黑按 union_id 删那一行', async () => {
        await h.blocklist.unblock('on_a');

        const remove = h.sqlOf('DELETE FROM "user_blacklist"');
        expect(remove.params).toEqual(['on_a']);
    });

    it('列出所有被拉黑的人', async () => {
        h.reply([{ UserBlacklist_union_id: 'on_a' }, { UserBlacklist_union_id: 'on_b' }]);

        expect(await h.blocklist.everyone()).toEqual(['on_a', 'on_b']);
    });
});

describe('common_message 那一行', () => {
    // role 是 `/session` 的判据之一（只有赤尾自己发的消息才有 session）。少读这一列，
    // 那条判断就只能永远为真或永远为假 —— 两个方向都不会报错。
    it('按 common_message_id 查，带回 role 与 response_id', async () => {
        h.reply([
            {
                CommonMessage_role: 'assistant',
                CommonMessage_response_id: 'sess-1',
            },
        ]);

        expect(await h.sessions.sessionOf('cm_1')).toEqual({
            role: 'assistant',
            responseId: 'sess-1',
        });

        const select = h.sqlOf('FROM "common_message"');
        expect(select.params).toEqual(['cm_1']);
    });

    it('查不到就是 null', async () => {
        h.reply([]);
        expect(await h.sessions.sessionOf('cm_1')).toBeNull();
    });

    it('那一行没有 response_id 时如实留空', async () => {
        h.reply([{ CommonMessage_role: 'user', CommonMessage_response_id: null }]);

        expect(await h.sessions.sessionOf('cm_1')).toEqual({
            role: 'user',
            responseId: undefined,
        });
    });
});
