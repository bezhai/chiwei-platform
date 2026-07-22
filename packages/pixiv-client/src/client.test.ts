import { describe, expect, it, mock } from 'bun:test';
import { PixivClient } from './client';

// 注入一个假的 axios 风格 httpClient：记录每次 post 的 body，返回同时满足
// pixivProxy（{error,body}）与 downloadContent（{code,msg}）两种调用的响应壳。
function makeMockHttp() {
    const calls: Array<{ url: string; body: any }> = [];
    const post = mock(async (url: string, body: any) => {
        calls.push({ url, body });
        return { data: { error: false, body: {}, code: 0, msg: 'ok' } };
    });
    return { post, calls };
}

const AUTH = { cookie: 'ck', user_agent: 'ua', sec_ch_ua: 'sec' };

async function withoutFollowerDelay<T>(run: () => Promise<T>): Promise<T> {
    const originalSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = ((callback: (...args: any[]) => void, _delay?: number, ...args: any[]) => {
        callback(...args);
        return 0 as any;
    }) as typeof globalThis.setTimeout;

    try {
        return await run();
    } finally {
        globalThis.setTimeout = originalSetTimeout;
    }
}

describe('PixivClient 关注列表分页', () => {
    it('请求所有包含尾页的 offset，并按页序聚合结果', async () => {
        for (const total of [24, 25, 48, 457]) {
            const offsets: number[] = [];
            const client = new PixivClient();

            (client as any).pixivProxy = mock(async (
                _baseUrl: string,
                _referer: string,
                params: { offset: number; limit: number },
            ) => {
                offsets.push(params.offset);
                const pageLength = Math.min(params.limit, total - params.offset);
                const users = Array.from({ length: pageLength }, (_, index) => {
                    const userId = String(params.offset + index);
                    return { userId, userName: `user-${userId}` };
                });

                return {
                    error: false,
                    message: '',
                    body: { total, users },
                };
            });

            const followers = await withoutFollowerDelay(() => client.getFollowersByTag('tag', '42'));
            const expectedOffsets = Array.from(
                { length: Math.ceil(total / 24) },
                (_, index) => index * 24,
            );
            const expectedUserIds = Array.from({ length: total }, (_, index) => String(index));

            expect(offsets).toEqual(expectedOffsets);
            expect(followers.map(({ userId }) => userId)).toEqual(expectedUserIds);
        }
    });
});

describe('PixivClient 鉴权头注入（pixiv_auth）', () => {
    it('pixivProxy：authProvider 有值时把 pixiv_auth 放进请求体', async () => {
        const http = makeMockHttp();
        const client = new PixivClient(
            { proxyHost: 'http://srv', httpSecret: 's', authProvider: async () => AUTH },
            http as any,
        );

        await client.pixivProxy('https://www.pixiv.net/ajax/x', 'ref', { lang: 'zh' });

        expect(http.calls).toHaveLength(1);
        expect(http.calls[0].url).toBe('http://srv/api/v2/proxy');
        expect(http.calls[0].body.referer).toBe('ref');
        expect(http.calls[0].body.url).toContain('https://www.pixiv.net/ajax/x');
        expect(http.calls[0].body.pixiv_auth).toEqual(AUTH);
    });

    it('pixivProxy：没有 authProvider 时不带 pixiv_auth 字段', async () => {
        const http = makeMockHttp();
        const client = new PixivClient(
            { proxyHost: 'http://srv', httpSecret: 's' },
            http as any,
        );

        await client.pixivProxy('https://www.pixiv.net/ajax/x', 'ref');

        expect(http.calls[0].body.pixiv_auth).toBeUndefined();
    });

    it('downloadContent：authProvider 有值时 pixiv_url 与 pixiv_auth 同在请求体', async () => {
        const http = makeMockHttp();
        const client = new PixivClient(
            { proxyHost: 'http://srv', httpSecret: 's', authProvider: async () => AUTH },
            http as any,
        );

        await client.downloadContent('https://i.pximg.net/img_p0.png');

        expect(http.calls[0].url).toBe('http://srv/api/v2/image-store/download');
        expect(http.calls[0].body.pixiv_url).toBe('https://i.pximg.net/img_p0.png');
        expect(http.calls[0].body.pixiv_auth).toEqual(AUTH);
    });

    it('authProvider 返回 undefined 时不带 pixiv_auth（交由 server 回退 Redis）', async () => {
        const http = makeMockHttp();
        const client = new PixivClient(
            { proxyHost: 'http://srv', httpSecret: 's', authProvider: async () => undefined },
            http as any,
        );

        await client.downloadContent('https://i.pximg.net/img_p0.png');

        expect(http.calls[0].body.pixiv_auth).toBeUndefined();
    });
});
