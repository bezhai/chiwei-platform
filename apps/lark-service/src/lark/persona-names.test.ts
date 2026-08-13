import { describe, expect, it } from 'bun:test';

import { loadLarkPersonaNames, type PersonaNameSource } from './persona-names';

function source(rows: Array<{ persona_id: string; display_name: string }>) {
    const queries: Array<{ sql: string; params: unknown[] }> = [];
    const db: PersonaNameSource = {
        query: async (sql, params) => {
            queries.push({ sql, params: params ?? [] });
            return rows;
        },
    };
    return { db, queries };
}

describe('loadLarkPersonaNames', () => {
    it('answers with the display name a persona wears', async () => {
        const { db } = source([{ persona_id: 'p_1', display_name: '赤尾' }]);
        const nameOf = await loadLarkPersonaNames(db, ['p_1']);
        expect(nameOf('p_1')).toBe('赤尾');
    });

    it('answers null for a persona it never heard of', async () => {
        const { db } = source([{ persona_id: 'p_1', display_name: '赤尾' }]);
        const nameOf = await loadLarkPersonaNames(db, ['p_1']);
        expect(nameOf('p_missing')).toBeNull();
    });

    it('asks for exactly the personas it was given, without repeats', async () => {
        const { db, queries } = source([]);
        await loadLarkPersonaNames(db, ['p_1', 'p_2', 'p_1']);
        expect(queries).toHaveLength(1);
        expect(queries[0]!.params).toEqual([['p_1', 'p_2']]);
    });

    // 一个 bot 都没绑人设时不该白跑一次查询。
    it('does not touch the database when no persona is wanted', async () => {
        const { db, queries } = source([]);
        const nameOf = await loadLarkPersonaNames(db, []);
        expect(queries).toEqual([]);
        expect(nameOf('p_1')).toBeNull();
    });

    it('reads only the two columns it needs', async () => {
        const { db, queries } = source([]);
        await loadLarkPersonaNames(db, ['p_1']);
        expect(queries[0]!.sql).toMatch(/select\s+persona_id,\s*display_name\s+from\s+bot_persona/i);
    });
});
