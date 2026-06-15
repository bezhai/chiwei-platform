import { DynamicConfig } from "@inner/shared";
import type { PixivAuth } from "@inner/pixiv-client";

// pixiv 鉴权字段在 Dynamic Config 里的 key（两仓共享契约的内网侧来源）。
export const PIXIV_COOKIE_KEY = "pixiv_cookie";
export const PIXIV_USER_AGENT_KEY = "pixiv_user_agent";
export const PIXIV_SEC_CH_UA_KEY = "pixiv_sec_ch_ua";

/**
 * 从 Dynamic Config 读 pixiv 鉴权字段（cookie / user-agent / sec-ch-ua）。
 * 只放非空字段；三项都空时返回 undefined —— 此时 worker 不带 pixiv_auth，
 * chiwei_bot_server 全量回退读自己的 Redis（Dynamic Config 还没配时的零回归路径）。
 */
export async function readPixivAuth(
  config: DynamicConfig,
): Promise<PixivAuth | undefined> {
  const [cookie, userAgent, secChUa] = await Promise.all([
    config.get(PIXIV_COOKIE_KEY),
    config.get(PIXIV_USER_AGENT_KEY),
    config.get(PIXIV_SEC_CH_UA_KEY),
  ]);

  const auth: PixivAuth = {};
  if (cookie) auth.cookie = cookie;
  if (userAgent) auth.user_agent = userAgent;
  if (secChUa) auth.sec_ch_ua = secChUa;

  return Object.keys(auth).length > 0 ? auth : undefined;
}

// 生产单例：DynamicConfig 自带 10s 缓存，cron 无 per-request context、默认读 prod lane。
const dynamicConfig = new DynamicConfig();

/** 给 PixivClient 的 authProvider 用。 */
export function getPixivAuth(): Promise<PixivAuth | undefined> {
  return readPixivAuth(dynamicConfig);
}
