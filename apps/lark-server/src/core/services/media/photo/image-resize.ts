import { Readable } from 'stream';
import { processImage } from '@integrations/tool-service/image-client';

/**
 * 处理图片用于飞书上传：缩放到飞书推荐尺寸
 *
 * 飞书图片上传限制约 10MB，缩放到 2048x2048 以内是安全的。
 */
export async function resizeImage(
    fileBuffer: Buffer,
): Promise<{ outFile: Readable; imgWidth: number; imgHeight: number }> {
    try {
        const { data, width, height } = await processImage(fileBuffer, {
            maxWidth: 2048,
            maxHeight: 2048,
        });

        const readable = new Readable();
        readable.push(data);
        readable.push(null);

        return { outFile: readable, imgWidth: width, imgHeight: height };
    } catch (error) {
        console.warn('tool-service 缩放失败，返回原图:', error);
        const readable = new Readable();
        readable.push(fileBuffer);
        readable.push(null);

        return { outFile: readable, imgWidth: 0, imgHeight: 0 };
    }
}
