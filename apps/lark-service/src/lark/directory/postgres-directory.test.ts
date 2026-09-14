import { describe, expect, it } from 'bun:test';
import type { DataSource, EntityManager } from 'typeorm';
import { postgresLarkDirectoryStore } from './postgres-directory';

function rig() {
    const calls: Array<{
        sql: string;
        params: unknown[];
        inTransaction: boolean;
    }> = [];
    const lifecycle: string[] = [];
    let answers: unknown[][] = [];
    let failAt: string | undefined;
    function manager(inTransaction: boolean): EntityManager {
        return {
            query: async (sql: string, params: unknown[]) => {
                calls.push({ sql, params, inTransaction });
                if (failAt && sql.includes(failAt))
                    throw new Error('database unavailable');
                return (sql.startsWith('SELECT') &&
                    !sql.includes('advisory')) ||
                    sql.includes('RETURNING union_id')
                    ? (answers.shift() ?? [])
                    : [];
            },
        } as unknown as EntityManager;
    }
    const source = {
        manager: manager(false),
        transaction: async <T>(run: (manager: EntityManager) => Promise<T>) => {
            lifecycle.push('begin');
            try {
                const result = await run(manager(true));
                lifecycle.push('commit');
                return result;
            } catch (error) {
                lifecycle.push('rollback');
                throw error;
            }
        },
    } as unknown as DataSource;
    return {
        calls,
        lifecycle,
        store: postgresLarkDirectoryStore(source),
        answers(v: unknown[][]) {
            answers = v;
        },
        failAt(value: string) {
            failAt = value;
        },
    };
}

describe('Postgres directory store', () => {
    it('does not rewrite existing nonempty profile or linked identity names', async () => {
        const h = rig();
        h.answers([[]]);
        await h.store.fillProfile({unionId: 'on_1', name: '晚到的旧姓名'});
        expect(h.calls).toHaveLength(4);
        const sql = h.calls[3]!.sql;
        expect(sql).toContain('WHERE lark_user.name IS NULL OR btrim(lark_user.name)');
        expect(sql).toContain('RETURNING union_id');
        expect(h.calls.some(call => call.sql.startsWith('UPDATE'))).toBe(false);
    });
    it('acquires a transaction advisory lock before updating and commits after applying', async () => {
        const h = rig();
        h.answers([[{union_id: "on_1"}]]);
        await h.store.withChatLock("oc_'quoted", async (tx) => {
            expect(h.lifecycle).toEqual(['begin']);
            expect(h.calls[0]!.sql).toContain("SET LOCAL lock_timeout = '5s'");
            expect(h.calls[1]!.sql).toContain('statement_timeout');
            expect(h.calls[2]!.sql).toContain(
                'idle_in_transaction_session_timeout',
            );
            expect(h.calls[3]!.sql).toContain('pg_advisory_xact_lock');
            expect(h.calls[3]!.params).toEqual(["lark-directory:oc_'quoted"]);
            expect(h.calls[0]!.sql).not.toContain('quoted');
            await tx.fillProfile({ unionId: 'on_1', name: "O'Brien" });
            await tx.applyMembership('oc_1', 'on_gone', true, new Date(2000));
        });
        expect(h.lifecycle).toEqual(['begin', 'commit']);
        expect(h.calls.every((call) => call.inTransaction)).toBe(true);
        const writes = h.calls.map((call) => call.sql).join('\n');
        expect(writes).not.toMatch(/is_admin|is_owner|is_manager/);
        expect(writes).not.toContain("O'Brien");
        expect(
            h.calls.find((call) =>
                call.sql.startsWith('INSERT INTO lark_user '),
            )!.params,
        ).toEqual(['on_1', "O'Brien", null, false]);
        expect(
            h.calls.find((call) =>
                call.sql.startsWith('INSERT INTO lark_group_member'),
            )!.params,
        ).toEqual(['oc_1', 'on_gone', true, new Date(2000)]);
        expect(writes).not.toMatch(
            /INSERT INTO (common_user|lark_user_open_id)/,
        );
    });

    it('rolls back all directory writes when any identity name update fails', async () => {
        const h = rig();
        h.failAt('UPDATE common_user');
        h.answers([[{union_id: 'on_1'}]]);
        await expect(
            h.store.withChatLock('oc_1', async (tx) => {
                await tx.fillProfile({ unionId: 'on_1', name: '新人' });
                await tx.applyMembership('oc_1', 'on_1', false, new Date(2000));
            }),
        ).rejects.toThrow('database unavailable');
        expect(h.lifecycle).toEqual(['begin', 'rollback']);
        expect(
            h.calls.some((call) =>
                call.sql.includes('INSERT INTO lark_group_member'),
            ),
        ).toBe(false);
    });

    it('releases the transaction on an API failure inside the lock', async () => {
        const h = rig();
        await expect(
            h.store.withChatLock('oc_1', async () => {
                throw new Error('API denied');
            }),
        ).rejects.toThrow('API denied');
        expect(h.lifecycle).toEqual(['begin', 'rollback']);
        expect(h.calls).toHaveLength(4);
    });

    it('updates private profiles and existing identity names in one transaction', async () => {
        const h = rig();
        h.answers([[{union_id: 'on_1'}]]);
        await h.store.fillProfile({
            unionId: 'on_1',
            name: '真人',
            avatarOrigin: 'https://avatar',
            openId: 'ou_1',
        });
        expect(h.lifecycle).toEqual(['begin', 'commit']);
        expect(h.calls).toHaveLength(6);
        expect(h.calls[3]!.params).toEqual([
            'on_1',
            '真人',
            'https://avatar',
            true,
        ]);
        expect(h.calls[4]!.sql).toContain('WHERE union_id = $1');
        expect(h.calls[5]!.sql).toContain(
            'l.common_user_id = u.common_user_id',
        );
    });

    it('reads legacy UTC timestamps explicitly and preserves null membership as active', async () => {
        const h = rig();
        h.answers([
            [
                {
                    union_id: 'on_human',
                    name: null,
                    is_leave: null,
                    updated_at: new Date(2000),
                },
            ],
        ]);
        expect(await h.store.member('oc_1', 'on_human')).toEqual({
            unionId: 'on_human',
            name: null,
            hasLeft: false,
            updatedAt: new Date(2000),
        });
        expect(h.calls[0]!.sql).toContain(
            "m.updated_at AT TIME ZONE 'UTC' AS updated_at",
        );
        expect(h.calls[0]!.params).toEqual(['oc_1', 'on_human']);
    });

    it('uses a conditional atomic upsert for newer evidence, with equal-time leave priority and unknown leave tombstones', async () => {
        const h = rig();
        h.answers([[{ union_id: 'on_new' }], []]);
        expect(
            await h.store.applyMembership(
                'oc_1',
                'on_new',
                true,
                new Date(2000),
            ),
        ).toBe(true);
        expect(
            await h.store.applyMembership(
                'oc_1',
                'on_new',
                false,
                new Date(1000),
            ),
        ).toBe(false);
        const sql = h.calls[0]!.sql;
        expect(sql).toContain('INSERT INTO lark_group_member');
        expect(sql).toContain('ON CONFLICT (chat_id, union_id) DO UPDATE');
        expect(sql).toContain("$4::timestamptz AT TIME ZONE 'UTC'");
        expect(sql).toContain('lark_group_member.updated_at IS NULL');
        expect(sql).toContain(
            'lark_group_member.updated_at < EXCLUDED.updated_at',
        );
        expect(sql).toContain(
            'lark_group_member.updated_at = EXCLUDED.updated_at',
        );
        expect(sql).toContain('EXCLUDED.is_leave = true');
        expect(sql).toContain(
            'lark_group_member.is_leave IS DISTINCT FROM true',
        );
        expect(sql).not.toMatch(/is_admin|is_owner|is_manager/);
        expect(h.calls[0]!.params).toEqual([
            'oc_1',
            'on_new',
            true,
            new Date(2000),
        ]);
    });
});
