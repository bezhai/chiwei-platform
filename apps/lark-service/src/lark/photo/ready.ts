// 凑够 N 张**飞书发得出去**的图。
//
//     图库候选 ──▶ 有 image_key？ ──是──▶ 直接用
//                       │否
//                       └──▶ 取字节 → 缩 → 传飞书 → 把 key 回写进库
//
// 卡片上放的是飞书的 image_key，不是 URL。所以"找 6 张图"实际上是"一直取到有 6 张
// 拿得到 image_key 为止"—— 候选会失败（对象存储里那个文件没了、飞书拒收一种格式），
// 失败一张就得补一张。
//
// ## 循环的三条终止条件，一条都不能少
//
//   凑够了            正常出口
//   图库说没有更多了   `exhausted`，再问也是空
//   一轮下来毫无进展   既没试到新地址、游标也没动。**没有它就是死循环打库** ——
//                     一个回空页又不说自己到头的图库会让这个循环一直转
//
// ## 补页必须带上"已经试过谁"
//
// 随机模式每一轮都是重新抽样，不排除已试过的地址的话，同一张坏图会被反复抽到，循环
// 只能靠上面第三条兜底停下来 —— 结果是"找 6 张"经常只返回 1 张。

import type { ImageForLark, ListPixivImageDto } from '@inner/pixiv-client';

import { dedupePixivAddrs, type LarkPhotoLibrary, type PhotoCursor } from './library';
import type { PhotoResize } from './resize';

export interface ReadyPhotoDeps {
    library: LarkPhotoLibrary;
    resize: PhotoResize;
    /** 传一张图，拿飞书的 image_key。平台没给 key 时返回 null（见 outbound/lark-api.ts）。 */
    upload: (bytes: Buffer) => Promise<string | null>;
}

/**
 * 指令、卡片回调、定时任务三个入口共用的那一跳。
 *
 * 交出来的每一张**保证有 image_key**，所以卡片层不必再判一次（判了也无从补救）。
 */
export type LarkReadyPhotos = (query: ListPixivImageDto) => Promise<ImageForLark[]>;

/** 游标的可比较形式。用来判断"这一轮到底动没动"。 */
function cursorKey(cursor: PhotoCursor | undefined): string {
    if (!cursor) return '';
    if (cursor.mode === 'explicit') return `explicit:${cursor.offset}`;
    const updateTime =
        cursor.updateTime instanceof Date
            ? cursor.updateTime.toISOString()
            : String(cursor.updateTime);
    return `ordered:${updateTime}:${String(cursor.id)}`;
}

export function readyPhotos(deps: ReadyPhotoDeps): LarkReadyPhotos {
    return async (query) => {
        const target = Math.max(1, query.page_size || 6);
        const attempted = new Set<string>();
        const ready: ImageForLark[] = [];
        // 「查看详情」那条路：交回来的顺序必须是卡片上那批地址的顺序。
        const wantedOrder = query.pixiv_addrs
            ? new Map(dedupePixivAddrs(query.pixiv_addrs).map((addr, at) => [addr, at]))
            : null;
        let cursor: PhotoCursor | undefined;

        while (ready.length < target) {
            const previousCursor = cursorKey(cursor);
            const previousAttempts = attempted.size;

            const page = await deps.library.candidates({
                query,
                // 要的是"还差几张"，不是 page_size：补页时多要等于白下载。
                limit: target - ready.length,
                cursor,
                excluded: [...attempted],
            });
            cursor = page.cursor;

            for (const photo of page.photos) {
                if (attempted.has(photo.pixiv_addr)) continue;
                // 图库可能把不在显式列表里的图也带回来（比如分组阶段的旁落）。
                if (wantedOrder && !wantedOrder.has(photo.pixiv_addr)) continue;
                attempted.add(photo.pixiv_addr);

                const usable = await onLark(deps, photo);
                if (usable) ready.push(usable);
                if (ready.length >= target) break;
            }

            if (ready.length >= target || page.exhausted) break;
            if (attempted.size === previousAttempts && cursorKey(cursor) === previousCursor) {
                console.error(
                    '[lark-photo] the library made no progress this round; stopping the refill',
                );
                break;
            }
        }

        if (wantedOrder) {
            ready.sort(
                (left, right) =>
                    (wantedOrder.get(left.pixiv_addr) ?? Number.MAX_SAFE_INTEGER) -
                    (wantedOrder.get(right.pixiv_addr) ?? Number.MAX_SAFE_INTEGER),
            );
        }
        return ready;
    };
}

/**
 * 让这一张变成飞书发得出去的样子。**不抛**：一张图不行不该带走整批，返回 null 表示
 * "这张不算数"，循环会去补。
 *
 * 回写在最后一步，而且**它失败这一张也算不成数**——图其实已经在飞书那边了。这是拆分
 * 前的形态（回写就在同一个 try 里），照搬，登记在案。
 */
async function onLark(deps: ReadyPhotoDeps, photo: ImageForLark): Promise<ImageForLark | null> {
    if (photo.image_key) return photo;

    try {
        if (!photo.tos_file_name) {
            console.error(`[lark-photo] ${photo.pixiv_addr} has no object name in the library`);
            return null;
        }

        const original = await deps.library.bytes(photo.tos_file_name);
        if (original.length === 0) {
            console.error(`[lark-photo] ${photo.tos_file_name} came back empty`);
            return null;
        }

        const resized = await deps.resize(original);
        const imageKey = await deps.upload(resized.bytes);
        if (!imageKey) {
            console.error(`[lark-photo] lark took ${photo.pixiv_addr} but gave no image_key`);
            return null;
        }

        const usable = {
            ...photo,
            image_key: imageKey,
            width: resized.width,
            height: resized.height,
        };
        await deps.library.noteLarkImageKey({
            pixiv_addr: photo.pixiv_addr,
            image_key: imageKey,
            width: resized.width,
            height: resized.height,
        });
        return usable;
    } catch (error) {
        console.error(`[lark-photo] could not make ${photo.pixiv_addr} sendable:`, error);
        return null;
    }
}
