// 出站要用到的飞书 OpenAPI，就这四件事。
//
// 这是一个**端口**：它描述"能对飞书做什么"，不描述"怎么做"。真身是 sdk-lark-api.ts
// （本服务里唯一持有飞书 SDK 客户端的地方），测试用手写替身。
//
// 范围刻意窄：出站只发富文本、只撤回、只传图。飞书 SDK 那个客户端还有几十个方法
// （建群、拉人、查历史……），端口里一个都不列 —— 端口列出来的每一项都是一条要维护的
// 契约，列了没人走的路等于凭空多出维护面。
//
// ## 失败行为（整个端口统一）
//
// **四个方法一律抛，不返回错误码、不吞。** 飞书返回非 0 code、SDK 抛 HTTP 错误、网络
// 断了，都变成一个 Error 往外走。要不要重试、要不要 ACK 由调用方决定 —— 这一层没有
// 足够的信息做那个决定（同一个"发失败了"，在首次投递时该重试，在已经发过一半的分段
// 消息里重试就会重复发）。
//
// 唯一的例外是 uploadImage 的"没拿到 key"，见它自己的注释。

import type { PostContent } from './post-content';

/** 飞书收下一条新消息之后给的东西。 */
export interface LarkSentMessage {
    /**
     * 飞书给这条新消息的 message_id。
     *
     * **可能没有。** 平台确实回过 code=0 但 data 里没带 id 的情况，此处如实留空而不是
     * 兜成空串 —— 落库那一侧要区分"发出去了但不知道 id"和"根本没发"，兜底会把这两件
     * 事拍成同一个值。
     */
    messageId?: string;
}

export interface LarkOutboundApi {
    /** 往一个会话里新发一条富文本消息。chatId 是飞书的 chat_id。 */
    sendPost(chatId: string, content: PostContent): Promise<LarkSentMessage>;

    /**
     * 回复一条已经存在的消息。messageId 是被回复那条的飞书 message_id。
     *
     * inThread=true 时飞书会把回复挂进话题（thread）而不是普通引用回复。
     */
    replyPost(
        messageId: string,
        content: PostContent,
        inThread: boolean,
    ): Promise<LarkSentMessage>;

    /**
     * 撤回一条消息。
     *
     * 消息已经被撤过、或者超出飞书允许撤回的时限时，飞书返回非 0 code，这里**抛**。
     * 「已经撤过了」在业务上往往等价于成功，但那是业务的判断，不是这一层的。
     */
    recall(messageId: string): Promise<void>;

    /**
     * 上传一张图片，拿飞书的 image_key。
     *
     * **平台没给 key 时返回 null 而不是抛**，因为这是唯一一个"失败了也能继续"的调用：
     * 上层会把这张图降级成一句文字，整条消息照发。真正的异常（网络断、鉴权失败）还是
     * 抛，由上层统一降级。
     */
    uploadImage(image: Buffer): Promise<string | null>;
}
