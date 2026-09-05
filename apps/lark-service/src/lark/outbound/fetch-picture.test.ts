import { describe, expect, it } from 'bun:test';

import {
    httpPictureDownload,
    toolServicePictureUrl,
    type ToolServicePost,
} from './fetch-picture';

interface PostSpy {
    post: ToolServicePost;
    calls: Array<{ path: string; body: unknown; headers: Record<string, string> }>;
}

function postSpy(reply: unknown | (() => never)): PostSpy {
    const spy: PostSpy = {
        calls: [],
        async post(path, body, headers) {
            spy.calls.push({ path, body, headers });
            if (typeof reply === 'function') (reply as () => never)();
            return { data: reply };
        },
    };
    return spy;
}

const SIGNED = { success: true, data: { url: 'https://tos.example/a.png?sig=x' }, message: 'ok' };

describe('toolServicePictureUrl', () => {
    it('拿句柄去 /api/image-pipeline/get-url 换一个可下载地址', async () => {
        // 端点和请求体是**跨服务契约**：写错了对面 404 / 422，而这一路的失败是降级，
        // 症状就只是"图老发不出来"。
        const spy = postSpy(SIGNED);
        const url = await toolServicePictureUrl({ post: spy.post, innerSecret: 's3cret' })(
            'pictures/a.png',
        );

        expect(url).toBe('https://tos.example/a.png?sig=x');
        expect(spy.calls).toHaveLength(1);
        expect(spy.calls[0]!.path).toBe('/api/image-pipeline/get-url');
        expect(spy.calls[0]!.body).toEqual({ file_name: 'pictures/a.png' });
    });

    it('带上内网口令', async () => {
        // 缺了发出的是 `Bearer undefined`，tool-service 401。
        const spy = postSpy(SIGNED);
        await toolServicePictureUrl({ post: spy.post, innerSecret: 's3cret' })('a.png');
        expect(spy.calls[0]!.headers.Authorization).toBe('Bearer s3cret');
    });

    it('信封说失败：返回 null，不抛', async () => {
        // 上层据 null 降级成一行文字。抛出去会让整条消息进重试。
        const spy = postSpy({ success: false, data: null, message: 'no such object' });
        expect(
            await toolServicePictureUrl({ post: spy.post, innerSecret: 's' })('gone.png'),
        ).toBeNull();
    });

    it('信封说成功但没给地址：返回 null', async () => {
        const spy = postSpy({ success: true, data: {}, message: 'ok' });
        expect(await toolServicePictureUrl({ post: spy.post, innerSecret: 's' })('a.png')).toBeNull();
    });

    it('对面回了个不认识的东西：返回 null', async () => {
        const spy = postSpy('<html>502 Bad Gateway</html>');
        expect(await toolServicePictureUrl({ post: spy.post, innerSecret: 's' })('a.png')).toBeNull();
    });

    it('这一跳自己炸了：**抛**，由 pictures.ts 统一降级', async () => {
        // 只有本文件知道"对面明说没有"，只有 pictures.ts 知道该说哪句降级文案 ——
        // 在这里吞掉异常会把"服务挂了"和"这张图没了"混成同一个现象。
        const spy = postSpy(() => {
            throw new Error('connect ECONNREFUSED');
        });
        expect(toolServicePictureUrl({ post: spy.post, innerSecret: 's' })('a.png')).rejects.toThrow(
            'ECONNREFUSED',
        );
    });
});

describe('httpPictureDownload', () => {
    it('按签出来的地址取字节', async () => {
        const seen: string[] = [];
        const download = httpPictureDownload(async (url) => {
            seen.push(url);
            return {
                ok: true,
                status: 200,
                arrayBuffer: async () => new TextEncoder().encode('image-bytes').buffer,
            };
        });

        expect(await download('https://tos.example/a.png?sig=x')).toEqual(
            Buffer.from('image-bytes'),
        );
        expect(seen).toEqual(['https://tos.example/a.png?sig=x']);
    });

    it('对面不给：抛，状态码带在报错里', async () => {
        const download = httpPictureDownload(async () => ({
            ok: false,
            status: 403,
            arrayBuffer: async () => new ArrayBuffer(0),
        }));
        expect(download('https://tos.example/expired.png')).rejects.toThrow('403');
    });
});
