import { Readable } from 'stream';

/**
 * 处理图片用于飞书上传（图片压缩功能待 tool-service 就绪后恢复）
 *
 * 当前降级实现：直接返回原图，不做缩放。
 */
export async function resizeImage(
    fileBuffer: Buffer,
): Promise<{ outFile: Readable; imgWidth: number; imgHeight: number }> {
    const readable = new Readable();
    readable.push(fileBuffer);
    readable.push(null);

    return {
        outFile: readable,
        imgWidth: 0,
        imgHeight: 0,
    };
}
