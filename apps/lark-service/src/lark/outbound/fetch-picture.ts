// 把她那张图从对象存储取下来：先向 tool-service 现签一个地址，再按地址下载字节。
//
// 两半都是纯基础设施 —— 一次内网 HTTP，一次公网下载。**降级说辞、并发上限、"一张
// 挂了不连坐"全在 pictures.ts**，本文件只负责把两跳做对，以及把"对面明说没有"和
// "这一跳自己炸了"分成两种答案（null 与抛）。
//
// ## 端点是跨服务契约
//
// `/api/image-pipeline/get-url` 由 tool-service 提供（app/api/image_pipeline.py），
// 收 `{file_name}`、回 `{success, data: {url, file_name}, message}` 这个信封。这条
// 路径在本文件里只出现一次，并且有测试钉着：写错了对面 404，而这一路的失败是降级，
// 症状就只是"她的图老发不出来"，不会有任何东西变红。
//
// lark-service 本来就在打 tool-service（入站附件走 `/api/image-pipeline/process`，
// 见 lark/attachments.ts）。所以这是加一个调用，不是加一条链路 —— 只不过入站那侧在
// 入口进程，本文件在出站进程，两个进程各自要配 `INNER_HTTP_SECRET`（见 config.ts）。

import type { PictureDownload, PictureUrlSigning } from './pictures';

/** tool-service 的现签端点。 */
const GET_URL_PATH = '/api/image-pipeline/get-url';

/**
 * 打 tool-service 的那一跳。真身是 laneRouter 建的 axios 客户端（它注 x-ctx-lane
 * 与 trace），返回值取它的 `data` —— 也就是下面那个信封。
 */
export type ToolServicePost = (
    path: string,
    body: { file_name: string },
    headers: Record<string, string>,
) => Promise<{ data: unknown }>;

export interface ToolServicePictureUrlDeps {
    post: ToolServicePost;
    /** 调 tool-service 的内网口令。缺了发出的是 `Bearer undefined`，对面 401。 */
    innerSecret: string | undefined;
}

/** tool-service 的响应信封。字段名照抄对面，不做驼峰翻译。 */
interface GetUrlEnvelope {
    success?: boolean;
    data?: { url?: string } | null;
    message?: string;
}

export function toolServicePictureUrl(deps: ToolServicePictureUrlDeps): PictureUrlSigning {
    return async (fileName) => {
        // 这一跳失败**不吞**：ECONNREFUSED 与"这张图没了"是两件事，混成同一个现象
        // 之后排查只能靠猜。上层（pictures.ts）拿它们说两句不同的降级文案。
        const response = await deps.post(
            GET_URL_PATH,
            { file_name: fileName },
            { Authorization: `Bearer ${deps.innerSecret}` },
        );

        const envelope = response.data as GetUrlEnvelope | undefined;
        if (!envelope?.success || !envelope.data?.url) {
            console.warn(
                `[lark-outbound] tool-service would not sign ${fileName}: ` +
                    `${envelope?.message ?? 'unrecognised response'}`,
            );
            return null;
        }
        return envelope.data.url;
    };
}

/**
 * 下载那一面用到的 HTTP 表面，就这三样。
 *
 * 不写成 `typeof fetch`：那个类型上还挂着 preconnect 之类的运行时私货，替身得跟着
 * 编一份才通得过编译，而它们跟下载图片一点关系都没有。
 */
export type LarkPictureFetch = (
    url: string,
    init?: { signal: AbortSignal },
) => Promise<{
    ok: boolean;
    status: number;
    arrayBuffer(): Promise<ArrayBuffer>;
}>;

/**
 * 一张图最多下多久。
 *
 * **这是护栏，不是可配置的业务行为**：它管的是"这一跳最多占用多久"，跟发不发图、发
 * 哪张都无关，所以不进 Dynamic Config。
 *
 * 时限盖住**整趟**，不只是建连那一下。对面完全可以接了连接、回一个 200、然后 body
 * 就是不结束 —— 那种情况下 `arrayBuffer()` 永远不 resolve，"一张图挂了不连累整条回
 * 复"这条降级一步都走不到，她那句话跟着一起吊死在这儿。
 */
const PICTURE_DOWNLOAD_TIMEOUT_MS = 15_000;

export function httpPictureDownload(
    fetchPicture: LarkPictureFetch = fetch,
    timeoutMs: number = PICTURE_DOWNLOAD_TIMEOUT_MS,
): PictureDownload {
    return async (url) => {
        const abort = new AbortController();
        let timer: ReturnType<typeof setTimeout> | undefined;

        const work = (async () => {
            const response = await fetchPicture(url, { signal: abort.signal });
            if (!response.ok) {
                // 抛出去，由 pictures.ts 统一降级 —— 只有它知道降级文案该说什么，也
                // 只有它知道这张图挂了不该连累整条回复。
                throw new Error(`failed to download picture: HTTP ${response.status}`);
            }
            return Buffer.from(await response.arrayBuffer());
        })();
        // 到点之后 abort 会让上面这趟自己炸一次，而那时 race 已经不听它了。挂个空
        // handler 只是别让它变成 unhandled rejection 把进程带走。
        work.catch(() => {});

        try {
            return await Promise.race([
                work,
                new Promise<never>((_, reject) => {
                    timer = setTimeout(() => {
                        abort.abort();
                        reject(
                            new Error(`picture download timed out after ${timeoutMs}ms`),
                        );
                    }, timeoutMs);
                }),
            ]);
        } finally {
            clearTimeout(timer);
        }
    };
}
