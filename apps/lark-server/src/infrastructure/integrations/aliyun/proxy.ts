// Re-export from @inner/pixiv-client
import { createPixivClient, ListPixivImageDto, ImageForLark, ReportLarkUploadDto } from '@inner/pixiv-client';

// 创建 Pixiv 客户端实例
const pixivClient = createPixivClient({
    proxyHost: process.env.PIXIV_PROXY_HOST || 'http://www.yuanzhi.xyz',
    httpSecret: process.env.PROXY_HTTP_SECRET || '',
});

export async function getPixivImages(params: ListPixivImageDto): Promise<ImageForLark[]> {
    return pixivClient.getPixivImages(params);
}

export async function reportLarkUpload(params: ReportLarkUploadDto): Promise<void> {
    return pixivClient.reportLarkUpload(params);
}
