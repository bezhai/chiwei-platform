/**
 * tool-service 图片处理客户端
 */

import { laneRouter } from '@infrastructure/lane-router';

export interface ProcessImageOptions {
    maxWidth?: number;
    maxHeight?: number;
    quality?: number;
    format?: string;
}

export interface ProcessImageResult {
    data: Buffer;
    width: number;
    height: number;
}

/**
 * 调用 tool-service 处理图片（缩放 + 格式转换）
 */
export async function processImage(
    buffer: Buffer,
    options: ProcessImageOptions = {},
): Promise<ProcessImageResult> {
    const params = new URLSearchParams();
    if (options.maxWidth) params.set('max_width', String(options.maxWidth));
    if (options.maxHeight) params.set('max_height', String(options.maxHeight));
    if (options.quality) params.set('quality', String(options.quality));
    if (options.format) params.set('format', options.format);

    // Buffer<ArrayBufferLike> 不是合法的 BlobPart（Bun 类型要求 BlobPart 的底层
    // 是普通 ArrayBuffer，而 Buffer 的底层可能是 SharedArrayBuffer）。复制进一个
    // 全新 Uint8Array（必由普通 ArrayBuffer 支撑），得到合法 BlobPart。一次性图片
    // 上传，单次拷贝无性能问题。
    const fileBytes = Uint8Array.from(buffer);
    const formData = new FormData();
    formData.append('file', new Blob([fileBytes]), 'image.bin');

    const response = await laneRouter.fetch('tool-service', `/api/image/process?${params.toString()}`, {
        method: 'POST',
        body: formData,
    });

    if (!response.ok) {
        const text = await response.text();
        throw new Error(`tool-service image process failed (${response.status}): ${text}`);
    }

    const width = parseInt(response.headers.get('X-Image-Width') || '0', 10);
    const height = parseInt(response.headers.get('X-Image-Height') || '0', 10);
    const arrayBuffer = await response.arrayBuffer();

    return {
        data: Buffer.from(arrayBuffer),
        width,
        height,
    };
}
