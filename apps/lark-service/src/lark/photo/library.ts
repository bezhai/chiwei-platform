// 图库端口：赤尾发的图从哪儿来。
//
// 这是一个**端口**，描述"能问图库要什么"，不描述图库长什么样。真身是 pixiv-mirror.ts
// （一份存在另一个 Mongo 实例里的 pixiv 抓取镜像，图片字节在 MinIO 上），测试用手写替身。
//
// ## 为什么它跟 projection/tables.ts 不是同一个端口
//
// 那个端口描述的是"一条飞书消息进来要读写哪些行"，落在业务库上，写入要对得上 spec 的
// common_* 写入矩阵。这个端口描述的是"图片素材从哪儿取"，落在**另一个 Mongo 实例 + 对象
// 存储**上（连接串是 `PIXIV_IMAGE_MONGO_*` 一族，不是 `MONGO_HOST`），跟飞书没有一点
// 关系 —— 合成一个端口之后，"改一下发图的取图口径"会变成动整条入站投影的端口。
//
// ## 翻页为什么是游标而不是页码
//
// 取到的候选未必都能用：没有 image_key 的要先下载再上传飞书，这一步会失败（对象存储里
// 那个文件没了、飞书拒收）。于是"要 6 张"实际上是"一直取到凑够 6 张为止"，中间要能接着
// 上一次的位置继续，而页码在**每次都排除掉已试过的地址**之后就不再稳定了。

import type { ImageForLark, ListPixivImageDto, ReportLarkUploadDto } from '@inner/pixiv-client';

/**
 * 接着上一页的位置。两种模式对应两种翻页方式，**不能混用**：
 *
 *   ordered   按 (update_time, _id) 倒序往下走。混进显式模式会退回页码翻页，
 *             而排除集每轮都在变，同一批图会被反复翻到。
 *   explicit  「查看详情」那条路：地址是卡片上写死的一批，翻页就是在这个列表上取窗口。
 *
 * 接错了不会报错，只会静默取错段，所以真身在两处都直接抛（见 pixiv-mirror.ts）。
 */
export type PhotoCursor =
    | { mode: 'ordered'; updateTime: unknown; id: unknown }
    | { mode: 'explicit'; offset: number };

export interface PhotoPageRequest {
    /** 要什么图。形状沿用 @inner/pixiv-client 的 DTO —— 那是镜像库自己的契约。 */
    query: ListPixivImageDto;
    /** 这一页最多要几张。**是"还差几张"，不是 page_size**。 */
    limit: number;
    cursor?: PhotoCursor;
    /**
     * 这一轮已经试过的地址，别再给了。
     *
     * 没有它的话，随机模式下补页会反复抽到同一张失败的图，循环靠"没有进展"的兜底
     * 才停得下来。
     */
    excluded?: readonly string[];
}

export interface PhotoPage {
    photos: ImageForLark[];
    /** 下一页从哪儿接。随机模式没有稳定顺序，所以没有游标。 */
    cursor?: PhotoCursor;
    /** 库里没有更多了。为真时调用方**必须**停 —— 否则就是空转打库。 */
    exhausted: boolean;
}

export interface LarkPhotoLibrary {
    /** 一页候选。候选**不保证**已经有飞书的 image_key，那是 ready.ts 的事。 */
    candidates(request: PhotoPageRequest): Promise<PhotoPage>;

    /** 取原图字节。取不到就抛 —— 调用方按"这张不算数"处理，不是整批失败。 */
    bytes(tosFileName: string): Promise<Buffer>;

    /**
     * 记下飞书给这张图的 image_key。
     *
     * 这是发图这条链路上唯一的写入，也是它便宜的原因：下一次同一张图就不必再下载
     * 上传一遍。写失败不该让这次发图失败（图已经在飞书那边了），所以调用方吞它。
     */
    noteLarkImageKey(upload: ReportLarkUploadDto): Promise<void>;
}

/**
 * 按首次出现去重，空串丢掉。
 *
 * 显式地址那条路要它两次：拼查询时（`$in` 里重复项没意义）和排序时（卡片上的顺序
 * 就是去重之后的顺序）。两处算得不一样的话，交回来的图会跟卡片对不上号。
 */
export function dedupePixivAddrs(pixivAddrs: readonly string[]): string[] {
    const seen = new Set<string>();
    const result: string[] = [];
    for (const pixivAddr of pixivAddrs) {
        if (pixivAddr.length === 0 || seen.has(pixivAddr)) continue;
        seen.add(pixivAddr);
        result.push(pixivAddr);
    }
    return result;
}
