// 上传飞书之前先缩图。
//
// 飞书的图片上传大约 10MB 封顶，而镜像库里存的是原图（pixiv 的大图动辄超过）。缩到
// 2048x2048 以内是安全的，而且顺带拿到真实宽高 —— 卡片的左右分栏权重按宽高比算。
//
// ## 缩不了不是失败
//
// tool-service 挂了、拒了、超时了，一律**退回原图**继续往下走：大部分图飞书本来就收得
// 下，为一次缩图失败让整条发图黄掉不划算。代价是宽高交 0（我们不知道），而那个 0 会
// 一路传下去：回写进镜像库、再被 calcBestChunks 拿去算 `height/width`。这是拆分前就有
// 的形态，照搬，登记在案。

/** 打 tool-service 的那一跳。真身是 LaneRouter 的 fetch（它注 x-ctx-lane 与 trace）。 */
export type ToolServiceFetch = (path: string, init: RequestInit) => Promise<Response>;

/** 缩过之后的图。宽高是**缩之后的**，会被回写进镜像库。 */
export interface ResizedPhoto {
    bytes: Buffer;
    width: number;
    height: number;
}

export type PhotoResize = (bytes: Buffer) => Promise<ResizedPhoto>;

/** 飞书图片上传大约 10MB 封顶，2048 见方是安全余量。 */
const MAX_EDGE = 2048;

function sizeHeader(response: Response, name: string): number {
    return Number.parseInt(response.headers.get(name) || '0', 10) || 0;
}

export function toolServiceResize(fetch: ToolServiceFetch): PhotoResize {
    return async (bytes) => {
        try {
            const form = new FormData();
            // Buffer 的底层可能是 SharedArrayBuffer，那不是合法的 BlobPart。复制进一个
            // 普通 Uint8Array（必由普通 ArrayBuffer 支撑）再包 Blob。
            form.append('file', new Blob([Uint8Array.from(bytes)]), 'image.bin');

            const response = await fetch(
                `/api/image/process?max_width=${MAX_EDGE}&max_height=${MAX_EDGE}`,
                { method: 'POST', body: form },
            );
            if (!response.ok) {
                throw new Error(
                    `tool-service refused to resize the image (${response.status}): ` +
                        (await response.text()),
                );
            }

            return {
                bytes: Buffer.from(await response.arrayBuffer()),
                width: sizeHeader(response, 'X-Image-Width'),
                height: sizeHeader(response, 'X-Image-Height'),
            };
        } catch (error) {
            console.warn('[lark-photo] could not resize, sending the original instead:', error);
            return { bytes, width: 0, height: 0 };
        }
    };
}
