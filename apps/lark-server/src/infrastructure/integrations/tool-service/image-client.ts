/**
 * tool-service 图片处理客户端
 */

import { context } from '@middleware/context';

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

function getBaseUrl(): string {
    const host = process.env.TOOL_SERVICE_HOST || 'localhost';
    const port = process.env.TOOL_SERVICE_PORT || '8000';
    return `http://${host}:${port}`;
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

    const url = `${getBaseUrl()}/api/image/process?${params.toString()}`;

    const formData = new FormData();
    formData.append('file', new Blob([buffer]), 'image.bin');

    const headers: Record<string, string> = {};
    const traceId = context.getTraceId();
    if (traceId) headers['X-Trace-Id'] = traceId;
    const appName = context.getBotName();
    if (appName) headers['X-App-Name'] = appName;
    const lane = context.getLane();
    if (lane) headers['x-lane'] = lane;

    const response = await fetch(url, {
        method: 'POST',
        headers,
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
