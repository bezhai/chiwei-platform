// 赤尾说"看这张图"的时候，图还在对象存储里。这一步把它搬到飞书。
//
// 出站消息上带的是 `picture_file_names` —— 对象存储的**永久句柄**，不是地址。本文件
// 把每个句柄变成飞书的一行：现签一个能下载的地址 → 下载 → 上传飞书拿 image_key。
//
// ## 为什么是句柄不是地址，而签名在这里现签
//
// 预签名地址只活 1.5 小时。队列正常几秒就投完，但泳道队列的 TTL 降级、DLQ 重投都
// 可能隔很久 —— 拿一个过期签名发出去，失败是**静默的**（消息发得出去，图片点开是
// 坏的）。所以队列里传永久句柄，签名在最靠近使用它的这一刻生成。
//
// ## 图只从这里进来，正文里的图片语法一律不算数
//
// 上游正文由一个对话模型自由生成，它随手写出的 `![x](y.png)` 不是任何真实的飞书
// image_key；把它当 image_key 发出去，飞书拒收的是**整条消息**。所以正文那条路在
// post-content.ts 里被彻底堵死，img 节点只由本文件产出。
//
// ## 一条铁律：任何一步失败都降级成一行文字，**永不抛**
//
// 一张图挂了不该让整条回复发不出去 —— 抛出去的结果是整条消息进重试，真人什么都收
// 不到，而且重试大概率还是同样的失败。降级之后她那句话照常送到。
//
// 三种说法对应三种不同的失败，是给排查用的，不要合并：
//   取不到地址  tool-service 没签出来 —— 句柄不存在、或者对象已经不在了
//   上传失败    下载到了，飞书没给 image_key —— 图片格式/大小被飞书拒了
//   处理失败    中途抛异常 —— 现签那一跳 500、下载超时、网络断、鉴权过期
//
// 说的是"第 N 张图"而不是句柄本身：句柄是对象存储的内部路径，摆给真人看既没有意义
// 也是多余的泄露；而一条消息里两张图都挂的时候，位置正是唯一能把它们分开的东西。

import type { LarkOutboundApi } from './lark-api';
import type { PostNode } from './post-content';

/**
 * 把一个永久句柄换成此刻可下载的地址。
 *
 * 签不出来返回 null（对面明说失败），网络/HTTP 层的意外**抛**，由本文件统一降级。
 */
export type PictureUrlSigning = (fileName: string) => Promise<string | null>;

/** 按签出来的地址取字节。失败**抛**，由本文件统一降级。 */
export type PictureDownload = (url: string) => Promise<Buffer>;

/** 上传那一步。只要 LarkOutboundApi 的一个方法，不要整个端口。 */
export type LarkImageUploader = Pick<LarkOutboundApi, 'uploadImage'>;

export interface LarkPictureDeps {
    sign: PictureUrlSigning;
    download: PictureDownload;
    uploader: LarkImageUploader;
}

/**
 * 同时在飞的下载/上传上限。
 *
 * 一次带十几张图不罕见。无上限并发会同时打满出网带宽，也容易撞飞书的上传限流 ——
 * 而撞了限流的后果是这一批图**全部**降级，比慢几百毫秒难看得多。
 */
const CONCURRENCY = 5;

/**
 * 把这一段要带出去的图变成飞书富文本的若干行（图各自成行）。
 *
 * 每个句柄产出恰好一行，顺序与传进来的一致：成功是 img 节点，失败是一行降级文字。
 * 空清单产出空数组 —— 没有图的那条路一次外部调用都不该发生。
 */
export async function larkPictureRows(
    fileNames: readonly string[],
    deps: LarkPictureDeps,
): Promise<PostNode[][]> {
    if (fileNames.length === 0) return [];

    const rows: PostNode[][] = [];
    for (let i = 0; i < fileNames.length; i += CONCURRENCY) {
        const batch = fileNames.slice(i, i + CONCURRENCY);
        const done = await Promise.all(
            batch.map((fileName, offset) => sendOne(fileName, i + offset + 1, deps)),
        );
        rows.push(...done);
    }
    return rows;
}

async function sendOne(
    fileName: string,
    position: number,
    deps: LarkPictureDeps,
): Promise<PostNode[]> {
    try {
        const url = await deps.sign(fileName);
        if (!url) {
            console.warn(`[lark-outbound] no signed url for picture ${fileName}`);
            return degraded(position, '取不到地址');
        }

        const imageKey = await deps.uploader.uploadImage(await deps.download(url));
        if (!imageKey) {
            console.error(`[lark-outbound] upload returned no image_key for ${fileName}`);
            return degraded(position, '上传失败');
        }
        return [{ tag: 'img', image_key: imageKey }];
    } catch (error) {
        console.error(`[lark-outbound] error sending picture ${fileName}:`, error);
        return degraded(position, '处理失败');
    }
}

function degraded(position: number, what: string): PostNode[] {
    return [{ tag: 'md', text: `(第 ${position} 张图${what})` }];
}
