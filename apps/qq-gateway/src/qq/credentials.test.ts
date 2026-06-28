import { describe, it, expect } from 'bun:test';
import { loadQQCredentials } from './credentials';

function fakeQuery(rows: Array<Record<string, unknown>>) {
    const calls: Array<{ text: string; params: unknown[] }> = [];
    const query = async (text: string, params: unknown[]) => {
        calls.push({ text, params });
        return rows;
    };
    return { query, calls };
}

describe('loadQQCredentials', () => {
    it('returns appId/appSecret parsed from a credentials row', async () => {
        const { query } = fakeQuery([{ credentials: { app_id: 'a', app_secret: 's' } }]);
        const creds = await loadQQCredentials('chiwei', { query });
        expect(creds).toEqual({ appId: 'a', appSecret: 's' });
    });

    it('queries bot_config by bot_name with botName as a bound parameter', async () => {
        const { query, calls } = fakeQuery([{ credentials: { app_id: 'a', app_secret: 's' } }]);
        await loadQQCredentials('chiwei', { query });
        expect(calls).toHaveLength(1);
        expect(calls[0].params).toContain('chiwei');
        expect(calls[0].text).toMatch(/bot_config/i);
        expect(calls[0].text).toMatch(/credentials/i);
    });

    it('throws (mentioning botName) when the row does not exist', async () => {
        const { query } = fakeQuery([]);
        await expect(loadQQCredentials('chiwei', { query })).rejects.toThrow(/chiwei/);
    });

    it('throws when credentials is null', async () => {
        const { query } = fakeQuery([{ credentials: null }]);
        await expect(loadQQCredentials('chiwei', { query })).rejects.toThrow(/chiwei/);
    });

    it('throws when credentials is not an object', async () => {
        const { query } = fakeQuery([{ credentials: 'not-an-object' }]);
        await expect(loadQQCredentials('chiwei', { query })).rejects.toThrow(/chiwei/);
    });

    it('throws when app_id is missing', async () => {
        const { query } = fakeQuery([{ credentials: { app_secret: 's' } }]);
        await expect(loadQQCredentials('chiwei', { query })).rejects.toThrow(/chiwei/);
    });

    it('throws when app_secret is missing', async () => {
        const { query } = fakeQuery([{ credentials: { app_id: 'a' } }]);
        await expect(loadQQCredentials('chiwei', { query })).rejects.toThrow(/chiwei/);
    });
});
