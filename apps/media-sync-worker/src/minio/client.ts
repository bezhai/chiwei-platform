import * as Minio from 'minio';

let client: Minio.Client | null = null;

/**
 * 懒加载的自建 MinIO 客户端（单例），用作内网训练缓存。
 *
 * endPoint 是 DNS 主机名（如 minio.prod），不带 scheme、不是 IP。
 * 所有连接参数从环境变量读取。
 */
export function getMinioClient(): Minio.Client {
    if (!client) {
        client = new Minio.Client({
            endPoint: process.env.MINIO_ENDPOINT!,
            port: Number.parseInt(process.env.MINIO_PORT ?? '9000', 10),
            useSSL: process.env.MINIO_USE_SSL === 'true',
            accessKey: process.env.MINIO_ACCESS_KEY!,
            secretKey: process.env.MINIO_SECRET_KEY!,
        });
    }
    return client;
}

/**
 * MinIO 中 pixiv 图片缓存使用的 bucket 名（从环境变量读取，默认 pixiv）。
 */
export function getMinioBucket(): string {
    return process.env.MINIO_BUCKET ?? 'pixiv';
}
