import { dynamicConfig, type DynamicConfigReader } from "../config/dynamicConfig";

// pixiv 下载用到的 Dynamic Config key，与 pixiv_cookie 等鉴权 key 同一前缀。
export const PIXIV_VERSION_KEY = "pixiv_version";
export const PIXIV_BAN_ILLUSTS_KEY = "pixiv_ban_illusts";
export const PIXIV_BAN_USERS_KEY = "pixiv_ban_users";
export const PIXIV_SKIP_WORDS_KEY = "pixiv_skip_words";

/** consumer 判断一个作品要不要下载时用的名单 */
export interface DownloadFilters {
  bannedUsers: string[];
  skipWords: string[];
}

/**
 * 读一个名单。值必须是 JSON 字符串数组，"[]" 表示名单为空。
 *
 * key 缺失、不是合法 JSON、不是字符串数组都抛错。SDK 拉取失败时返回的是默认值 ''，
 * 和 key 不存在分不开；如果当成空名单，配置服务一出故障，被 ban 的内容就会被静默下载。
 */
export async function readStringList(config: DynamicConfigReader, key: string): Promise<string[]> {
  const raw = await config.get(key);
  if (raw === "") {
    throw new Error(`Dynamic Config ${key} is missing`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`Dynamic Config ${key} is not valid JSON`);
  }

  if (!Array.isArray(parsed) || !parsed.every((item) => typeof item === "string")) {
    throw new Error(`Dynamic Config ${key} must be a JSON array of strings`);
  }
  return parsed;
}

/** pixiv ajax 的 version 参数；未配置时返回 null，请求不带这个参数。 */
export async function readPixivVersion(config: DynamicConfigReader): Promise<string | null> {
  const version = await config.get(PIXIV_VERSION_KEY);
  return version === "" ? null : version;
}

export async function readDownloadFilters(config: DynamicConfigReader): Promise<DownloadFilters> {
  const [bannedUsers, skipWords] = await Promise.all([
    readStringList(config, PIXIV_BAN_USERS_KEY),
    readStringList(config, PIXIV_SKIP_WORDS_KEY),
  ]);
  return { bannedUsers, skipWords };
}

export interface PixivSettingCheck {
  key: string;
  ok: boolean;
  detail: string;
}

/** 部署验证用：逐个 key 读一遍并报告结果，不抛错。 */
export async function checkPixivSettings(config: DynamicConfigReader): Promise<PixivSettingCheck[]> {
  const version = await readPixivVersion(config);
  const results: PixivSettingCheck[] = [
    {
      key: PIXIV_VERSION_KEY,
      ok: true,
      detail: version === null ? "unset (requests omit version)" : `version=${version}`,
    },
  ];
  for (const key of [PIXIV_BAN_ILLUSTS_KEY, PIXIV_BAN_USERS_KEY, PIXIV_SKIP_WORDS_KEY]) {
    try {
      const list = await readStringList(config, key);
      results.push({ key, ok: true, detail: `${list.length} entries` });
    } catch (err) {
      results.push({ key, ok: false, detail: err instanceof Error ? err.message : String(err) });
    }
  }
  return results;
}

export function getBanIllusts(): Promise<string[]> {
  return readStringList(dynamicConfig, PIXIV_BAN_ILLUSTS_KEY);
}

export function getDownloadFilters(): Promise<DownloadFilters> {
  return readDownloadFilters(dynamicConfig);
}
