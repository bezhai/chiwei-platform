// 赤尾说"看这张图"的时候，图还在别处。这一步把它搬到飞书。
//
// 上游（画图、找图的那些能力）产出图片之后不会直接给飞书 —— 它不认识飞书，也不该
// 认识。它把图片存到对象存储，然后在一张**注册表**里记一行「1.png → 这个 URL」，正文
// 里只留一个 `![描述](1.png)` 的占位引用。本文件负责把占位引用换成飞书自己的
// image_key：查注册表 → 下载 → 传给飞书。
//
// ## 一条铁律：任何一步失败都降级成一句文字，**永不抛**
//
// 图挂了不该让整条回复发不出去。用户看见"（图片 1.png 不可用）"至少知道她想给你看
// 什么、也知道出了岔子；抛出去的结果是整条消息进重试，用户什么都收不到，而且重试
// 大概率还是同样的失败。
//
// 三种说法对应三种不同的失败，是给排查用的，不要合并：
//   不可用    注册表里根本没这一行 —— 上游没注册，或者用错了注册表 id
//   上传失败  下载到了，飞书没给 image_key —— 图片格式/大小被飞书拒了
//   处理失败  中途抛异常 —— 下载超时、网络断、鉴权过期
//
// 降级之后正文里就没有图片语法了，所以最后一步（markdownToPostContent）看到的是纯
// 文字，不会再产出 img 节点。

import type { LarkOutboundApi } from './lark-api';

/**
 * markdown 里的占位引用。`@` 前缀可有可无 —— 两种写法上游都产出过。
 *
 * 只认 `数字.png` 这一种文件名：正文里还会有模型自己编的外链图片语法，那些不该被
 * 当成注册表引用去查（查了必定 miss，然后被降级成"不可用"，等于凭空多出一句错误
 * 提示）。它们由 markdownToPostContent 静默跳过。
 */
const IMAGE_REF_PATTERN = /!\[([^\]]*)\]\(@?(\d+\.png)\)/g;

/**
 * 同时在飞的下载/上传上限。
 *
 * 一条消息里塞十几张图不罕见。无上限并发会同时打满出网带宽，也容易撞飞书的上传限流 ——
 * 而撞了限流的后果是这一批图**全部**降级，比慢几百毫秒难看得多。
 */
const CONCURRENCY = 5;

/** 这次出站能用的图片，以及怎么把它们取下来。 */
export interface LarkImageRegistry {
    /**
     * 这张注册表里都有什么：文件名 → 可下载的 URL。
     *
     * **registryId 是全局消息 id**，不是飞书那边的裸 id —— 注册表是上游按全局 id 写的。
     * 整张表不存在时返回 null（正常情况：这条消息本来就没有图）。
     */
    lookup(registryId: string): Promise<Record<string, string> | null>;

    /** 下载一张图。失败**抛**，由本文件统一降级。 */
    download(url: string): Promise<Buffer>;
}

/** 上传那一步。只要 LarkOutboundApi 的一个方法，不要整个端口。 */
export type LarkImageUploader = Pick<LarkOutboundApi, 'uploadImage'>;

export interface LarkImageDeps {
    registry: LarkImageRegistry;
    uploader: LarkImageUploader;
}

/**
 * 把正文里的占位引用全部换掉，返回新的正文。
 *
 * registryId 缺失时**原样返回**：没有注册表就无从解析，留着占位符比编一句错误提示好 ——
 * 最后一步会把它静默跳过，用户看到的是一条没有图的正常回复。
 */
export async function resolveImageReferences(
    markdown: string,
    registryId: string | undefined,
    deps: LarkImageDeps,
): Promise<string> {
    IMAGE_REF_PATTERN.lastIndex = 0;
    const refs = [...markdown.matchAll(IMAGE_REF_PATTERN)];
    if (refs.length === 0) return markdown;
    if (!registryId) return markdown;

    const registry = await deps.registry.lookup(registryId);
    if (!registry || Object.keys(registry).length === 0) {
        console.warn(
            `[lark-outbound] no image registry for id=${registryId}; leaving refs unresolved`,
        );
        return markdown;
    }

    let result = markdown;
    for (let i = 0; i < refs.length; i += CONCURRENCY) {
        const batch = refs.slice(i, i + CONCURRENCY);
        const resolved = await Promise.all(
            batch.map((ref) => resolveOne(ref[0], ref[1], ref[2], registry, deps)),
        );
        for (const { ref, replacement } of resolved) {
            // 替换串用函数：字符串形式里 `$&` 之类是"整个匹配"的占位符，而 alt 是模型
            // 写的、什么都可能有。
            result = result.replace(ref, () => replacement);
        }
    }
    return result;
}

async function resolveOne(
    ref: string,
    alt: string,
    filename: string,
    registry: Record<string, string>,
    deps: LarkImageDeps,
): Promise<{ ref: string; replacement: string }> {
    const url = registry[filename];
    if (!url) {
        console.warn(`[lark-outbound] image ${filename} not in registry`);
        return { ref, replacement: `(图片 ${filename} 不可用)` };
    }

    try {
        const imageKey = await deps.uploader.uploadImage(await deps.registry.download(url));
        if (!imageKey) {
            console.error(`[lark-outbound] upload returned no image_key for ${filename}`);
            return { ref, replacement: `(图片 ${filename} 上传失败)` };
        }
        return { ref, replacement: `![${alt}](${imageKey})` };
    } catch (error) {
        console.error(`[lark-outbound] error resolving ${filename}:`, error);
        return { ref, replacement: `(图片 ${filename} 处理失败)` };
    }
}
