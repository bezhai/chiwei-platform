// 上传飞书之前先把图缩到飞书收得下的尺寸。这一步打 tool-service。

import { describe, expect, it } from 'bun:test';

import { toolServiceResize, type ToolServiceFetch } from './resize';

function respondWith(
    body: number[],
    headers: Record<string, string>,
    status = 200,
): ToolServiceFetch {
    return async () => new Response(new Blob([new Uint8Array(body)]), { status, headers });
}

describe('缩图', () => {
    it('把字节 POST 给 tool-service，尺寸上限写在 query 里', async () => {
        const seen: { path: string; init: RequestInit }[] = [];
        const resize = toolServiceResize(async (path, init) => {
            seen.push({ path, init });
            return new Response(new Uint8Array([1, 2]), {
                headers: { 'X-Image-Width': '800', 'X-Image-Height': '1200' },
            });
        });

        await resize(Buffer.from('original'));

        expect(seen[0]!.path).toBe('/api/image/process?max_width=2048&max_height=2048');
        expect(seen[0]!.init.method).toBe('POST');
        const form = seen[0]!.init.body as FormData;
        expect(form).toBeInstanceOf(FormData);
        expect(await (form.get('file') as Blob).text()).toBe('original');
    });

    it('缩好的字节和尺寸从响应体与响应头取', async () => {
        const resize = toolServiceResize(
            respondWith([7, 7], {
                'X-Image-Width': '800',
                'X-Image-Height': '1200',
            }),
        );

        const resized = await resize(Buffer.from('original'));

        expect([...resized.bytes]).toEqual([7, 7]);
        expect(resized.width).toBe(800);
        expect(resized.height).toBe(1200);
    });

    // 缩不了不该让整次发图失败：原图往往飞书也收得下。宽高交 0 是拆分前的形态 ——
    // 它会被原样回写进镜像库，也会让卡片的分栏权重算成 NaN（登记在案，本批不改）。
    it('tool-service 拒了就退回原图，宽高交 0', async () => {
        const resize = toolServiceResize(respondWith([], {}, 500));

        const resized = await resize(Buffer.from('original'));

        expect(resized.bytes.toString()).toBe('original');
        expect(resized.width).toBe(0);
        expect(resized.height).toBe(0);
    });

    it('打不通也退回原图', async () => {
        const resize = toolServiceResize(async () => {
            throw new Error('connect ECONNREFUSED');
        });

        const resized = await resize(Buffer.from('original'));

        expect(resized.bytes.toString()).toBe('original');
        expect(resized.width).toBe(0);
    });

    // 响应头缺了不是失败：图缩好了，只是尺寸不知道。
    it('响应头没带尺寸时算 0，不抛', async () => {
        const resize = toolServiceResize(respondWith([9], {}));

        const resized = await resize(Buffer.from('original'));

        expect([...resized.bytes]).toEqual([9]);
        expect(resized.width).toBe(0);
        expect(resized.height).toBe(0);
    });
});
