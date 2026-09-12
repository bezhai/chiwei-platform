// 入站图片在对象存储里叫什么。
//
// 每个渠道收到图片之后都把它交给 tool-service 的 `/api/image-pipeline/process`，那条
// 管线压过之后存进对象存储，名字是 `temp/<file_key>.jpg`（tool-service
// `app/services/image_pipeline.py`）。**这是跨服务的存储命名契约**，本文件是 TS 这侧
// 唯一一份 —— 飞书和 QQ 两条入站都从这里取，各写一遍的话，两个渠道会在不同的位置上
// 找同一张图，而且两边代码各自看着都对。
//
// 各渠道交给管线的 file_key 不是同一种东西（飞书是 image_key，QQ 是附件的公网地址），
// 但名字按同一条规则算 —— 规则认的是"交给管线的那个 key"，不是渠道。
//
// 算出来的名字**不保证对象此刻就在那儿**：入站缓存是旁路（失败只记日志），而且是异步
// 的。所以它写进 content 之后，取图那一步仍然要真取一次。

/** 交给图片管线的那个 key 对应的对象名。key 为空时抛 —— 见下。 */
export function inboundImageObject(fileKey: string): string {
    const key = fileKey.trim();
    if (key.length === 0) {
        // 空 key 算出来的是 `temp/.jpg`：一个所有空 key 共用的位置。写进 content 之后
        // 它看起来跟一个正常的位置没有区别，而它指向的东西跟这条消息毫无关系。
        throw new Error('inboundImageObject needs a non-empty file key');
    }
    return `temp/${key}.jpg`;
}
