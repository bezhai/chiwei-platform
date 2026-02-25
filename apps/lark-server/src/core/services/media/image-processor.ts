import { downloadResource, downloadSelfImage, uploadImage } from '@integrations/lark-client';
import { getTos } from '@volcengine/tos';
import { cache } from '@cache/cache-decorator';
import { RedisLock } from '@cache/redis-lock';
import { get as redisGet, setWithExpire as redisSetWithExpire } from '@cache/redis-client';
import { Readable } from 'node:stream';
import sharp from 'sharp';

/**
 * 图片处理请求接口
 */
export interface ImageProcessRequest {
    message_id?: string;
    file_key: string;
}

/**
 * base64 图片上传请求接口
 */
export interface Base64ImageUploadRequest {
    base64_data: string;
}

/**
 * 图片处理响应接口
 */
export interface ImageProcessResponse {
    success: boolean;
    data?: {
        url: string;
        file_key: string;
    };
    message: string;
    error_code?: string;
}

/**
 * base64 图片上传响应接口
 */
export interface Base64ImageUploadResponse {
    success: boolean;
    data?: {
        image_key: string;
    };
    message: string;
    error_code?: string;
}

/**
 * 图片处理错误类
 */
export class ImageProcessError extends Error {
    constructor(
        message: string,
        public code: string,
        public statusCode: number = 500,
    ) {
        super(message);
        this.name = 'ImageProcessError';
    }
}

/**
 * 图片处理服务类
 */
export class ImageProcessorService {
    private static instance: ImageProcessorService;

    private constructor() {}

    static getInstance(): ImageProcessorService {
        if (!ImageProcessorService.instance) {
            ImageProcessorService.instance = new ImageProcessorService();
        }
        return ImageProcessorService.instance;
    }

    /**
     * 处理图片：下载并上传到OSS
     */
    async processImage(request: ImageProcessRequest): Promise<ImageProcessResponse> {
        const { message_id, file_key } = request;

        console.info(`开始处理图片: message_id=${message_id}, file_key=${file_key}`);

        try {
            // 第一层缓存：检查文件是否已上传（7天缓存）
            let fileName = await this.checkFileUploaded(file_key);

            if (!fileName) {
                // 文件未上传，使用带锁的方法处理上传
                fileName = await this.ensureFileUploaded(request);
            } else {
                console.debug(
                    `文件已上传，跳过下载和上传: file_key=${file_key}, fileName=${fileName}`,
                );
            }

            // 第二层缓存：生成URL（1小时缓存）
            const url = await this.getFileUrlWithCache(fileName);

            console.info(`图片处理成功: ${url}`);

            return {
                success: true,
                data: {
                    url,
                    file_key,
                },
                message: '图片处理成功',
            };
        } catch (error) {
            console.error(`图片处理失败: message_id=${message_id}, file_key=${file_key}`, error);
            throw this.handleError(error);
        }
    }

    /**
     * 处理 base64 图片上传到飞书
     */
    async uploadBase64Image(request: Base64ImageUploadRequest): Promise<Base64ImageUploadResponse> {
        const { base64_data } = request;

        console.info(`开始处理 base64 图片上传`);

        try {
            // 将 base64 转换为 stream
            const imageStream = this.base64ToStream(base64_data);

            // 上传到飞书获取 image_key
            const uploadResult = await uploadImage(imageStream);

            if (!uploadResult || !uploadResult.image_key) {
                throw new ImageProcessError(
                    '飞书上传失败，未获取到 image_key',
                    'UPLOAD_FAILED',
                    500,
                );
            }

            const imageKey = uploadResult.image_key;
            console.info(`base64 图片上传成功，image_key: ${imageKey}`);

            return {
                success: true,
                data: {
                    image_key: imageKey,
                },
                message: 'base64 图片上传成功',
            };
        } catch (error) {
            console.error(`base64 图片上传失败`, error);
            throw this.handleError(error);
        }
    }

    /**
     * 将 base64 字符串转换为 Readable 流
     */
    private base64ToStream(base64Data: string): Readable {
        try {
            // 移除 data:image/...;base64, 前缀
            const base64Content = base64Data.replace(/^data:image\/[a-zA-Z]+;base64,/, '');

            // 将 base64 转换为 Buffer
            const buffer = Buffer.from(base64Content, 'base64');

            // 创建 Readable 流
            const stream = new Readable();
            stream.push(buffer);
            stream.push(null); // 标记流结束

            console.debug(`base64 转换为流成功，数据大小: ${buffer.length} bytes`);
            return stream;
        } catch (error) {
            throw new ImageProcessError(
                `base64 转换失败: ${error instanceof Error ? error.message : '未知错误'}`,
                'BASE64_CONVERT_ERROR',
                400,
            );
        }
    }

    /**
     * 确保文件已上传（带Redis锁防止并发上传）
     */
    @RedisLock({
        key: (request: ImageProcessRequest) => `image_upload_lock:${request.file_key}`,
        ttl: 60, // 60秒锁定时间
        timeout: 30000, // 30秒超时
        retryInterval: 200, // 200ms重试间隔
    })
    private async ensureFileUploaded(request: ImageProcessRequest): Promise<string> {
        const { message_id, file_key } = request;

        console.debug(`获取上传锁成功，开始处理: file_key=${file_key}`);

        // 再次检查缓存，可能在等待锁的过程中已被其他请求上传
        let fileName = await this.checkFileUploaded(file_key);
        if (fileName) {
            console.debug(`等待锁期间文件已上传: file_key=${file_key}, fileName=${fileName}`);
            return fileName;
        }

        console.debug(`确认文件未上传，开始下载和上传: file_key=${file_key}`);

        // 下载图片
        const imageStream = await this.downloadImage(file_key, message_id);

        // 上传到OSS
        fileName = await this.uploadToOssOnly(file_key, imageStream);

        // 记录到第一层缓存
        await this.recordFileUploaded(file_key, fileName);

        console.debug(`文件上传完成: file_key=${file_key}, fileName=${fileName}`);
        return fileName;
    }

    /**
     * 检查文件是否已上传（第一层缓存）
     */
    private async checkFileUploaded(fileKey: string): Promise<string | null> {
        const cacheKey = `image_upload:${fileKey}`;
        const fileName = await redisGet(cacheKey);
        return fileName;
    }

    /**
     * 记录文件已上传（第一层缓存，7天TTL）
     */
    private async recordFileUploaded(fileKey: string, fileName: string): Promise<void> {
        const cacheKey = `image_upload:${fileKey}`;
        const ttl = 7 * 24 * 60 * 60; // 7天
        await redisSetWithExpire(cacheKey, fileName, ttl);
    }

    /**
     * 获取文件URL（第二层缓存，10分钟TTL）
     */
    @cache({ type: 'redis', ttl: 600 }) // 10分钟缓存
    private async getFileUrlWithCache(fileName: string): Promise<string> {
        return await getTos().getFileUrl(fileName);
    }

    /**
     * 从Lark下载图片
     */
    private async downloadImage(fileKey: string, messageId?: string): Promise<Readable> {
        try {
            let downloadResponse;
            if (messageId) {
                // 通过 messageId 和 fileKey 下载
                downloadResponse = await downloadResource(messageId, fileKey, 'image');
            } else {
                // 仅通过 image_key 下载
                downloadResponse = await downloadSelfImage(fileKey);
            }
            const imageStream = downloadResponse.getReadableStream();

            if (!imageStream) {
                throw new ImageProcessError('无法获取图片流', 'DOWNLOAD_STREAM_ERROR', 400);
            }

            console.debug(`成功下载图片流: file_key=${fileKey}`);
            return imageStream;
        } catch (error) {
            if (error instanceof ImageProcessError) {
                throw error;
            }
            throw new ImageProcessError(
                `下载图片失败: ${error instanceof Error ? error.message : '未知错误'}`,
                'DOWNLOAD_ERROR',
                500,
            );
        }
    }

    /**
     * 上传图片到TOS（仅上传，不获取URL）
     */
    private async uploadToOssOnly(fileKey: string, imageStream: Readable): Promise<string> {
        try {
            const fileName = `temp/${fileKey}.jpg`;

            // 压缩图片
            const compressedBuffer = await this.compressImage(imageStream);

            await getTos().uploadFile(fileName, compressedBuffer);

            console.debug(`成功上传到TOS: ${fileName}`);
            return fileName;
        } catch (error) {
            if (error instanceof ImageProcessError) {
                throw error;
            }
            throw new ImageProcessError(
                `上传到TOS失败: ${error instanceof Error ? error.message : '未知错误'}`,
                'UPLOAD_ERROR',
                500,
            );
        }
    }

    /**
     * 压缩图片 - 简单版本
     */
    private async compressImage(imageStream: Readable): Promise<Buffer> {
        try {
            // 将流转为Buffer
            const chunks: Buffer[] = [];
            for await (const chunk of imageStream) {
                chunks.push(chunk);
            }
            const originalBuffer = Buffer.concat(chunks);

            // 使用Sharp压缩
            const compressedBuffer = await sharp(originalBuffer)
                .resize(1440, 1440, {
                    fit: 'inside',
                    withoutEnlargement: true,
                })
                .jpeg({
                    quality: 80,
                    progressive: true,
                })
                .toBuffer();

            console.debug(
                `图片压缩完成: ${originalBuffer.length} -> ${compressedBuffer.length} bytes`,
            );
            return compressedBuffer;
        } catch (error) {
            console.warn('图片压缩失败，使用原图:', error);

            // 压缩失败时，将流转为Buffer返回原图
            const chunks: Buffer[] = [];
            for await (const chunk of imageStream) {
                chunks.push(chunk);
            }
            return Buffer.concat(chunks);
        }
    }

    /**
     * 统一错误处理
     */
    private handleError(error: unknown): ImageProcessError {
        if (error instanceof ImageProcessError) {
            return error;
        }

        if (error instanceof Error) {
            return new ImageProcessError(`图片处理失败: ${error.message}`, 'PROCESSING_ERROR', 500);
        }

        return new ImageProcessError('图片处理失败: 未知错误', 'UNKNOWN_ERROR', 500);
    }
}

// 导出单例实例
export const imageProcessor = ImageProcessorService.getInstance();
