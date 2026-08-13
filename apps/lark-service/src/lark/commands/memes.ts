// 表情包服务：有哪些模板、以及现做一张。
//
// 它是一个独立部署的外部服务（`MEME_HOST:MEME_PORT`），我们只用它两个端点：
//
//     GET  /memes/list          有哪些模板（关键词、吃几张图）
//     POST /memes/{name}/       现做一张，multipart 上去、图片字节下来
//
// ## 列表带十分钟缓存，而且**键名是跨服务共享的**
//
// 拆分前那层缓存来自 `@cache({type:'redis', ttl:600})` 装饰器，键由装饰器按
// `${方法名}:${参数的 JSON}` 算出来，也就是字面的 `getMemeList:[]`。切换窗口里
// channel-server 和 lark-service 会同时在跑：键名不一致不会出错，只会让两边各去打一遍
// meme 服务（请求翻倍、命中率减半）。所以这个字面量照搬，不"顺手规范化"成带前缀的名字。
//
// 缓存的失败语义也照搬：**读不到就抛，不静默降级去打服务**。降级听起来更友好，但那是
// 上游没有的行为，而指令层本来就会把异常翻成一句人话。
//
// ## 渲染这一跳用 axios，不用 fetch
//
// 请求体是 multipart，而消息里带的图是 `downloadResource` 交回来的 Node **流**。
// `form-data` 那个包能直接吃流，Web 的 FormData 不能（要先整张读进内存）。响应是二进制。
// 这两件事一起决定了这一跳留在 axios 上。

import type { Readable } from 'node:stream';
import FormData from 'form-data';
import { createHttpClient } from '@inner/shared';
import { context } from '@inner/shared/middleware';

import type { LarkCommandCache } from '../rules/commands';

/** 一个表情包模板。字段名是 meme 服务的口径。 */
export interface LarkMeme {
    key: string;
    params_type: {
        min_images?: number;
        max_images?: number;
        min_texts?: number;
        max_texts?: number;
        default_texts?: string[];
    };
    keywords: string[];
}

/** 有哪些模板。 */
export type LarkMemeTemplates = () => Promise<LarkMeme[]>;

/** 现做一张，交回图片的字节。`images` 是消息里带的图（流，不先读进内存）。 */
export type LarkMemeRender = (
    name: string,
    texts: string[],
    images: Readable[],
    args: Record<string, string>,
) => Promise<Buffer>;

export interface LarkMemes {
    templates: LarkMemeTemplates;
    render: LarkMemeRender;
}

/** 拆分前那个装饰器算出来的键。**跨服务共享**，理由见文件头。 */
export const MEME_LIST_CACHE_KEY = 'getMemeList:[]';

/** 十分钟。照搬。 */
export const MEME_LIST_CACHE_SECONDS = 10 * 60;

/**
 * 给模板列表包一层缓存。
 *
 * 解析不了缓存里那串字时**抛**。上游这一格是"原样把那串字返回"，于是调用方拿到一个
 * string 当数组用、在 `.find` 上炸出一个 TypeError —— 可观测的落点跟这里完全一样（都是
 * 指令层那个 catch），只是报错说不清是怎么回事。库里的值是我们自己写的，这条路走不到。
 */
export function cachedMemeTemplates(
    cache: LarkCommandCache,
    templates: LarkMemeTemplates,
): LarkMemeTemplates {
    return async () => {
        const cached = await cache.get(MEME_LIST_CACHE_KEY);
        if (cached !== null) {
            try {
                return JSON.parse(cached) as LarkMeme[];
            } catch (error) {
                throw new Error(
                    `lark-service: the meme list cache holds something that is not JSON: ${
                        error instanceof Error ? error.message : String(error)
                    }`,
                );
            }
        }

        const fresh = await templates();
        await cache.setWithExpire(
            MEME_LIST_CACHE_KEY,
            JSON.stringify(fresh),
            MEME_LIST_CACHE_SECONDS,
        );
        return fresh;
    };
}

/**
 * 打 meme 服务的真身。
 *
 * @param baseUrl `MEME_HOST:MEME_PORT`。上游就是这么拼的（**host 里带着协议**，所以这里
 *   不补 `http://` —— 补了配置里写全的那种会变成 `http://http://…`）。
 */
export function httpMemes(baseUrl: string): LarkMemes {
    // 与 channel-server 的 `@http/client` 同一套：把 trace / bot / lane 带上，
    // meme 服务那边的日志才对得上是谁在调。
    const http = createHttpClient({
        timeout: 30_000,
        headerProvider: () => {
            const headers: Record<string, string> = {};
            const traceId = context.getTraceId();
            if (traceId) headers['X-Trace-Id'] = traceId;
            const botName = context.getBotName();
            if (botName) headers['X-App-Name'] = botName;
            const lane = context.getLane();
            if (lane) headers['x-ctx-lane'] = lane;
            return headers;
        },
    });

    return {
        async templates() {
            const response = await http.get<LarkMeme[]>(`${baseUrl}/memes/list`);
            return response.data;
        },

        async render(name, texts, images, args) {
            const form = new FormData();
            for (const text of texts) form.append('texts', text);
            // 没有参数时**不带这个字段**，与上游一致 —— 带一个 `{}` 上去，meme 服务那侧
            // 走的是另一条分支。
            if (Object.keys(args).length > 0) form.append('args', JSON.stringify(args));
            images.forEach((image, at) => form.append('images', image, `${at}.jpg`));

            const response = await http.post(`${baseUrl}/memes/${name}/`, form, {
                headers: { ...form.getHeaders() },
                responseType: 'arraybuffer',
                // 非 200 也要拿到响应体：meme 服务把"为什么做不出来"写在 body 的 detail
                // 里（文字太长、图片数量不对……），那句话是要原样回给用户的。axios 默认
                // 对 4xx/5xx 直接抛，抛掉的正是这句话。
                validateStatus: () => true,
            });

            if (response.status !== 200) throw new Error(detailOf(response.data));
            return Buffer.from(response.data as ArrayBuffer);
        },
    };
}

/** meme 服务把拒绝的理由写在 `detail` 里。读不出来就用一句通用的。 */
function detailOf(body: unknown): string {
    try {
        const parsed = JSON.parse(Buffer.from(body as ArrayBuffer).toString()) as {
            detail?: string;
        };
        if (parsed.detail) return parsed.detail;
    } catch (error) {
        console.error('[lark-meme] could not read why the meme service refused:', error);
    }
    return '生成表情包失败';
}
