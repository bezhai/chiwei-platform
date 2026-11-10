import { describe, expect, it } from 'bun:test';
import { DynamicConfig } from '@inner/shared';
import {
    PIXIV_BAN_ILLUSTS_KEY,
    PIXIV_BAN_USERS_KEY,
    PIXIV_SKIP_WORDS_KEY,
    PIXIV_VERSION_KEY,
    checkPixivSettings,
    readPixivVersion,
    readStringList,
} from './settings';

// Same contract as the real SDK's get(): a missing key, and a failed snapshot fetch,
// both come back as the default value ('' unless the caller passes one).
function fakeConfig(values: Record<string, string>) {
    return {
        get: async (key: string, defaultValue = ''): Promise<string> => values[key] ?? defaultValue,
    };
}

describe('readStringList', () => {
    it('parses a JSON array of strings', async () => {
        await expect(
            readStringList(fakeConfig({ [PIXIV_BAN_USERS_KEY]: '["1001","1002"]' }), PIXIV_BAN_USERS_KEY)
        ).resolves.toEqual(['1001', '1002']);
    });

    it('accepts "[]" as a deliberately empty list', async () => {
        await expect(
            readStringList(fakeConfig({ [PIXIV_SKIP_WORDS_KEY]: '[]' }), PIXIV_SKIP_WORDS_KEY)
        ).resolves.toEqual([]);
    });

    // A missing key is indistinguishable from a failed fetch, so it must never be
    // read as "nothing is banned".
    it('rejects a missing key', async () => {
        await expect(readStringList(fakeConfig({}), PIXIV_BAN_ILLUSTS_KEY)).rejects.toThrow(
            `Dynamic Config ${PIXIV_BAN_ILLUSTS_KEY} is missing`
        );
    });

    it('rejects when the real SDK cannot reach paas-engine', async () => {
        const unreachable = new DynamicConfig({
            paasEngineUrl: 'http://127.0.0.1:1',
            laneProvider: () => 'prod',
        });

        await expect(readStringList(unreachable, PIXIV_BAN_USERS_KEY)).rejects.toThrow(
            `Dynamic Config ${PIXIV_BAN_USERS_KEY} is missing`
        );
    });

    it('rejects a value that is not valid JSON', async () => {
        await expect(
            readStringList(fakeConfig({ [PIXIV_BAN_USERS_KEY]: '["1001",' }), PIXIV_BAN_USERS_KEY)
        ).rejects.toThrow(`Dynamic Config ${PIXIV_BAN_USERS_KEY} is not valid JSON`);
    });

    it.each([
        ['an array of numbers', '[1001, 1002]'],
        ['an array with a null', '["1001", null]'],
        ['an object', '{"1001": true}'],
        ['a bare string', '"1001"'],
        ['null', 'null'],
    ])('rejects %s', async (_case, value) => {
        await expect(
            readStringList(fakeConfig({ [PIXIV_BAN_USERS_KEY]: value }), PIXIV_BAN_USERS_KEY)
        ).rejects.toThrow(`Dynamic Config ${PIXIV_BAN_USERS_KEY} must be a JSON array of strings`);
    });
});

describe('readPixivVersion', () => {
    it('returns the configured version', async () => {
        await expect(readPixivVersion(fakeConfig({ [PIXIV_VERSION_KEY]: 'abc123' }))).resolves.toBe('abc123');
    });

    it('returns null when unset, so pixiv requests go out without a version parameter', async () => {
        await expect(readPixivVersion(fakeConfig({}))).resolves.toBeNull();
    });
});

describe('checkPixivSettings', () => {
    it('reports every key on its own without throwing, for the deployment connectivity check', async () => {
        const results = await checkPixivSettings(
            fakeConfig({
                [PIXIV_VERSION_KEY]: 'abc123',
                [PIXIV_BAN_ILLUSTS_KEY]: '["1","2"]',
                [PIXIV_SKIP_WORDS_KEY]: 'not json',
            })
        );

        expect(results).toEqual([
            { key: PIXIV_VERSION_KEY, ok: true, detail: 'version=abc123' },
            { key: PIXIV_BAN_ILLUSTS_KEY, ok: true, detail: '2 entries' },
            { key: PIXIV_BAN_USERS_KEY, ok: false, detail: `Dynamic Config ${PIXIV_BAN_USERS_KEY} is missing` },
            { key: PIXIV_SKIP_WORDS_KEY, ok: false, detail: `Dynamic Config ${PIXIV_SKIP_WORDS_KEY} is not valid JSON` },
        ]);
    });

    it('reports an unset version as fine, since requests then simply omit it', async () => {
        const [version] = await checkPixivSettings(fakeConfig({}));

        expect(version).toEqual({ key: PIXIV_VERSION_KEY, ok: true, detail: 'unset (requests omit version)' });
    });
});
