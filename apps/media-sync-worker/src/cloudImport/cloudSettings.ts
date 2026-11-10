/**
 * 读云端 Redis 里的三个名单和 version，原样返回给调用方，由主会话在用户同意后写进 Dynamic Config。
 * 这里只读，不写任何配置。
 */
import {
    PIXIV_BAN_ILLUSTS_KEY,
    PIXIV_BAN_USERS_KEY,
    PIXIV_SKIP_WORDS_KEY,
    PIXIV_VERSION_KEY,
} from '../pixiv/settings';
import type { CloudRedis } from './cloudSource';

export interface CloudList {
    /** 这份名单在 Dynamic Config 里对应的 key */
    dynamic_config_key: string;
    length: number;
    /** 写进 Dynamic Config 的 JSON 数组字符串的字节数，用来判断放进快照是否合适 */
    json_bytes: number;
    /** 按字典序排序 */
    values: string[];
}

export interface CloudSettings {
    ban_illusts: CloudList;
    ban_user: CloudList;
    skip_words: CloudList;
    version: { dynamic_config_key: string; value: string | null };
}

export async function readCloudSettings(redis: Pick<CloudRedis, 'smembers' | 'get'>): Promise<CloudSettings> {
    const [banIllusts, banUser, skipWords, version] = await Promise.all([
        redis.smembers('ban_illusts'),
        redis.smembers('ban_user'),
        redis.smembers('skip_words'),
        redis.get('version'),
    ]);
    return {
        ban_illusts: toList(PIXIV_BAN_ILLUSTS_KEY, banIllusts),
        ban_user: toList(PIXIV_BAN_USERS_KEY, banUser),
        skip_words: toList(PIXIV_SKIP_WORDS_KEY, skipWords),
        version: { dynamic_config_key: PIXIV_VERSION_KEY, value: version },
    };
}

function toList(key: string, members: string[]): CloudList {
    const values = [...members].sort();
    return {
        dynamic_config_key: key,
        length: values.length,
        json_bytes: Buffer.byteLength(JSON.stringify(values), 'utf8'),
        values,
    };
}
