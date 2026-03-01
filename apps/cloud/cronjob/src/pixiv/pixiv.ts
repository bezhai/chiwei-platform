import { PixivClient, createPixivClient, FollowerInfo, IllustrationPageDetail } from "@inner/pixiv-client";
import redisClient from "../redis/redisClient";
import { GetIllustInfoBody } from "../service/types";
import { cacheProxy } from "../utils/cache";

// 创建 Pixiv 客户端实例
const pixivClient = createPixivClient({
  proxyHost: process.env.PIXIV_PROXY_HOST || 'http://www.yuanzhi.xyz',
  httpSecret: process.env.HTTP_SECRET || '',
  defaultUserId: '35384654',
  getVersion: async () => redisClient.get("version"),
});

/**
 * 获取用户指定标签下的关注列表
 * @param tag 标签名称
 * @returns 关注者信息数组
 */
async function getFollowersByTag(tag: string): Promise<FollowerInfo[]> {
  return pixivClient.getFollowersByTag(tag);
}

/**
 * 根据用户 ID 获取该作者的所有作品 ID
 * @param userId 用户 ID
 * @returns 作者的所有作品 ID 数组
 */
async function getAuthorArtwork(userId: string): Promise<string[]> {
  return pixivClient.getAuthorArtwork(userId);
}

/**
 * 根据标签和页码获取作品 ID 列表
 * @param tag 标签名称
 * @param page 页码
 * @returns 作品 ID 数组
 */
async function getTagArtwork(tag: string, page: number): Promise<string[]> {
  return pixivClient.getTagArtwork(tag, page);
}

/**
 * 根据插画 ID 获取插画信息
 * @param illustId 插画的 ID
 * @returns 包含插画信息的对象
 * @throws 请求失败时抛出错误
 */
async function getIllustInfo(illustId: string): Promise<GetIllustInfoBody> {
  return pixivClient.getIllustInfo<GetIllustInfoBody>(illustId);
}

async function getIllustInfoWithCache(illustId: string): Promise<GetIllustInfoBody> {
  const cacheKey = `illustInfo_${illustId}`;  // 使用插画ID作为缓存键

  // 使用 cacheProxy 包裹请求逻辑
  return cacheProxy(cacheKey, async () => {
    return await getIllustInfo(illustId);
  });
}

/**
 * 根据插画 ID 获取插画的页面详情
 * @param illustId 插画的 ID
 * @returns 包含插画页面详情的数组
 * @throws 请求失败时抛出错误
 */
export async function getIllustPageDetail(illustId: string): Promise<IllustrationPageDetail[]> {
  return pixivClient.getIllustPageDetail(illustId);
}

export { getFollowersByTag, getAuthorArtwork, getTagArtwork, getIllustInfoWithCache, getIllustInfo };
