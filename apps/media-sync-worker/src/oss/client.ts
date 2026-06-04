import OSS from 'ali-oss';

let client: OSS | null = null;

/**
 * 懒加载的阿里云 OSS 读取客户端（单例）。
 *
 * 必须使用 OSS 公网/外网 endpoint —— 本 worker 运行在 k3s，不在阿里云内网，
 * 不能使用内网 endpoint。所有连接参数从环境变量读取。
 */
export function getOssClient(): OSS {
    if (!client) {
        client = new OSS({
            endpoint: process.env.END_POINT,
            accessKeyId: process.env.OSS_ACCESS_KEY_ID!,
            accessKeySecret: process.env.OSS_ACCESS_KEY_SECRET!,
            bucket: process.env.OSS_BUCKET,
            cname: true,
        });
    }
    return client;
}
