import { describe, expect, it } from 'bun:test';
import { createWorkerPixivClient } from './pixiv';
import { PIXIV_VERSION_KEY } from './settings';

function fakeConfig(values: Record<string, string>) {
    return {
        get: async (key: string, defaultValue = ''): Promise<string> => values[key] ?? defaultValue,
    };
}

function recordingHttp() {
    const urls: string[] = [];
    const http = {
        post: async (_url: string, body: { url: string }) => {
            urls.push(body.url);
            return { data: { error: false, message: '', body: { illusts: { '101': null } } } };
        },
    };
    return { http, urls };
}

describe('worker pixiv client reads pixiv_version from Dynamic Config', () => {
    it('omits the version parameter when pixiv_version is unset', async () => {
        const { http, urls } = recordingHttp();
        const client = createWorkerPixivClient({ config: fakeConfig({}), httpClient: http as any });

        await client.getAuthorArtwork('42');

        expect(urls).toHaveLength(1);
        expect(new URL(urls[0]).searchParams.has('version')).toBe(false);
    });

    it('sends the configured version', async () => {
        const { http, urls } = recordingHttp();
        const client = createWorkerPixivClient({
            config: fakeConfig({ [PIXIV_VERSION_KEY]: 'abc123' }),
            httpClient: http as any,
        });

        await client.getAuthorArtwork('42');

        expect(new URL(urls[0]).searchParams.get('version')).toBe('abc123');
    });
});
