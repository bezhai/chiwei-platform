// Re-export from @inner/pixiv-client for backward compatibility
import { createPixivClient } from "@inner/pixiv-client";

// 创建 Pixiv 客户端实例
const pixivClient = createPixivClient({
  proxyHost: process.env.PIXIV_PROXY_HOST || 'http://www.yuanzhi.xyz',
  httpSecret: process.env.HTTP_SECRET || '',
});

/**
 * pixivProxy - 发送带有 referer 和参数的 POST 请求
 * @param baseUrl 要请求的 Base URL
 * @param referer 请求头中的 Referer
 * @param params 请求的查询参数
 * @returns 响应体
 */
export async function pixivProxy<T>(
  baseUrl: string,
  referer: string,
  params: Record<string, any> = {}
): Promise<T> {
  return pixivClient.pixivProxy<T>(baseUrl, referer, params);
}

/**
 * getContent - 请求指定 URL 的内容
 * @param url 要请求的 Pixiv URL
 * @returns Promise<void> 表示成功或失败
 */
export async function getContent(url: string): Promise<void> {
  return pixivClient.downloadContent(url);
}

// Re-export types
export {
  ImageForLark,
  ListPixivImageDto,
  UploadImageToLarkDto,
  UploadLarkResp,
  BaseResponse,
  PaginationResponse,
  StatusMode,
} from "@inner/pixiv-client";
