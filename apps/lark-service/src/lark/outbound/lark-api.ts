// 本服务对飞书开放平台能做的全部动作。
//
// 这是一个**端口**：它描述"能对飞书做什么"，不描述"怎么做"。真身是 sdk-lark-api.ts
// （本服务里唯一持有飞书 SDK 客户端的地方），测试用手写替身。
//
// ## "出站"说的是方向，不是那个 Deployment
//
// 本文件曾经只有四个方法，因为当时只有 `lark-outbound` 那个进程在用它（发富文本、
// 撤回、传图）。指令、卡片回调、定时任务搬过来之后，用它的变成两个进程 —— 但做的
// 事情是同一件：**从我们这边打飞书的接口**。所以仍然是一个端口，不是两个：
//
//   * 两个进程用的是同一份按 bot 分池的客户端（见 sdk-lark-api.ts），拆成两个端口
//     等于两份池、每个 bot 两个 SDK 客户端、tenant token 各换各的；
//   * 失败语义只有一套（见下），拆开之后同一个名字下会并存两种，读的人无从判断。
//
// 拆端口的判据是**冲突/失败语义不同**（对比 projection/tables.ts 与 outbound/
// tables.ts 那两个：同一张表、相反的冲突处理，所以必须拆）。这里不满足那个判据。
//
// ## 范围：有人走的路才列
//
// 飞书 SDK 那个客户端有几十个方法，端口只列**本服务真的会走**的。端口列出来的每一
// 项都是一条要维护的契约，列了没人走的路等于凭空多出维护面。今天故意不在里面的：
// 建群 / 查群列表 / 查群资料 / 拉群成员名册 —— 那几件事属于群成员事件那条链，本服务
// 还没有认领它（入站事件处理表里只有消息接收和卡片回调）。真要认领时再加。
//
// ## 失败行为（整个端口统一）
//
// **一律抛，不返回错误码、不吞。** 飞书返回非 0 code、SDK 抛 HTTP 错误、网络断了，
// 都变成一个 Error 往外走。要不要重试、要不要 ACK、要不要对着用户说一句"失败了"，
// 由调用方决定 —— 这一层没有足够的信息做那个决定（同一个"发失败了"，在首次投递时
// 该重试，在已经发过一半的分段消息里重试就会重复发）。
//
// 两个例外，各自在自己的注释里写明理由：`uploadImage` 的"没拿到 key"、以及三个查询
// 方法的"查不到"（返回 null）。**查不到不是失败**，是一个要调用方处理的正常答案。

import type { Readable } from 'node:stream';

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

/**
 * 一张交互卡片的报文。
 *
 * **端口不解释它的形状。** 拼卡片是指令层的事（用 feishu-card 那个库），这里只负责把
 * 拼好的东西原样交给飞书。写成 `object` 而不是 `Record<string, unknown>`：卡片库产出
 * 的是类实例，类类型没有隐式索引签名，用后者会在每个调用点逼出一次无意义的断言。
 */
export type LarkCard = object;

/** 被 @ 的人在一条查回来的消息里的样子。 */
export interface LarkMessageMention {
    /** 正文里的占位符，形如 `@_user_1`。 */
    key?: string;
    id?: string;
    name?: string;
}

/**
 * 一条**查回来**的消息（不是事件推过来的那种）。
 *
 * 字段名用驼峰，与 LarkSentMessage 一致 —— 这是 API 端口，不是表端口，没有"跟写入
 * 矩阵逐字对上"那个约束（对比 projection/tables.ts 的列名口径）。
 */
export interface LarkMessageInfo {
    /**
     * **必填，而且是真的必填。** 平台回了一条却没带 id 时适配器当场抛（见
     * sdk-lark-api.ts 的 messageInfoOf），不兜一个 undefined 出来 —— 端口对查询只认
     * 「查不到返回 null」和「出错抛」两种答案，兜底会造出类型上是 string、运行期是
     * undefined 的第三种，而每个调用点都类型合法。
     */
    messageId: string;
    chatId?: string;
    /**
     * 谁发的。
     *
     * **bot 发的消息这里是 app_id，不是 union_id** —— 「撤回」指令就是拿它跟自己的
     * app_id 比，才知道这条是不是自己发的。所以 senderIdType 必须一起带上，光看 id
     * 分不出这是哪种身份。
     */
    senderId?: string;
    senderIdType?: string;
    senderType?: string;
    messageType?: string;
    /** 毫秒时间戳的字符串形式，与入站事件同一口径。 */
    createTime?: string;
    /** 正文的 JSON 串。形状随 messageType 变，端口不解析它。 */
    content?: string;
    mentions: LarkMessageMention[];
    rootId?: string;
    parentId?: string;
    threadId?: string;
    /** 已经被撤回/删除的消息**仍然会出现在历史里**，靠这一位区分。 */
    deleted?: boolean;
}

/** 查群历史的一页。 */
export interface LarkMessagePage {
    items: LarkMessageInfo[];
    hasMore: boolean;
    /** 下一页的游标。hasMore 为假时没有。 */
    pageToken?: string;
}

/** 查群历史问的问题。一次一页 —— 翻页与限流是调用方的事，见 listMessages。 */
export interface LarkMessageQuery {
    chatId: string;
    /** 秒级时间戳（飞书要字符串，端口收数，转换在真身里做一次）。 */
    startTime?: number;
    endTime?: number;
    pageToken?: string;
}

/** 一个人在飞书通讯录里的样子。 */
export interface LarkUserInfo {
    unionId?: string;
    openId?: string;
    name?: string;
    avatarOrigin?: string;
}

export interface LarkOutboundApi {
    // ---- 往一个会话里新发一条 ----

    /** 富文本。chatId 是飞书的 chat_id。 */
    sendPost(chatId: string, content: PostContent): Promise<LarkSentMessage>;

    /** 纯文本。 */
    sendText(chatId: string, text: string): Promise<LarkSentMessage>;

    /** 交互卡片。 */
    sendCard(chatId: string, card: LarkCard): Promise<LarkSentMessage>;

    /** 表情包。fileKey 是飞书表情的 key，不是图片的 image_key。 */
    sendSticker(chatId: string, fileKey: string): Promise<LarkSentMessage>;

    // ---- 挂在一条已有消息上回复 ----

    /**
     * 回复一条已经存在的消息。messageId 是被回复那条的飞书 message_id。
     *
     * inThread=true 时飞书会把回复挂进话题（thread）而不是普通引用回复。
     */
    replyPost(messageId: string, content: PostContent, inThread: boolean): Promise<LarkSentMessage>;

    /** 纯文本回复。指令出错时对着用户说的那一句走这里。 */
    replyText(messageId: string, text: string, inThread: boolean): Promise<LarkSentMessage>;

    /** 卡片回复。 */
    replyCard(messageId: string, card: LarkCard, inThread: boolean): Promise<LarkSentMessage>;

    /**
     * 图片回复。
     *
     * **没有 inThread** —— 飞书这条路不进话题，调用方也从来没有过这个需求。加一个永远
     * 传 false 的参数只是给每个调用点添一次要读懂的东西。
     */
    replyImage(messageId: string, imageKey: string): Promise<LarkSentMessage>;

    /**
     * 用飞书后台配好的卡片模板回复。
     *
     * 模板卡片是 interactive 的一个**子形状**，不是另一种 msg_type —— 包错了飞书直接
     * 拒收，而调用方看到的只是"这条没发出去"。所以包法定在真身里，不留给调用方。
     */
    replyTemplate(
        messageId: string,
        templateId: string,
        variables?: Record<string, unknown>,
    ): Promise<LarkSentMessage>;

    // ---- 撤回 ----

    /**
     * 撤回一条消息。
     *
     * 消息已经被撤过、或者超出飞书允许撤回的时限时，飞书返回非 0 code，这里**抛**。
     * 「已经撤过了」在业务上往往等价于成功，但那是业务的判断，不是这一层的 ——
     * 飞书那个数字码挂在抛出的 Error 上（@inner/lark-utils 的 larkErrorCode 读），
     * 业务层自己按码分支。recall.ts 认的就是「消息已被撤回或删除」那一个。
     */
    recall(messageId: string): Promise<void>;

    // ---- 查 ----

    /**
     * 按 message_id 查一条消息。**查不到返回 null**，不抛。
     *
     * 飞书这个接口返回的是一个列表（为了兼容合并转发），这里只取第一条 —— 按主键查
     * 本来就只该有一条，列表是接口形状不是语义。
     */
    getMessage(messageId: string): Promise<LarkMessageInfo | null>;

    /**
     * 查一个会话的历史消息，**一次一页**。
     *
     * 翻页和限流都不在这里：飞书对这个接口有 40/s + 800/min 的额度，而"翻多少页"是
     * 业务问题（水群统计要一整天，别的场景可能只要最近几条）。把整个循环塞进端口，
     * 一次调用背后就是不确定次数的 API 请求，调用方失去了控制权也失去了可观测性。
     */
    listMessages(query: LarkMessageQuery): Promise<LarkMessagePage>;

    /**
     * 按 union_id 查一个人。**查不到返回 null**。
     *
     * 注意"没有这个人"和"没权限看这个人"在飞书那边是两件事：后者返回非 0 code，会
     * 按端口的统一口径**抛**出来。`/bind` 就是靠这个区别把"人不存在"和"应用没通讯录
     * 权限"分开说的。
     */
    getUser(unionId: string): Promise<LarkUserInfo | null>;

    // ---- 改群 ----

    /**
     * 把一个人拉进群。openId 是飞书的 open_id（不是 union_id）。
     *
     * 用在退群自动拉回：管理员 `/bind` 过的人退群时再拉回来。
     */
    addChatMember(chatId: string, openId: string): Promise<void>;

    // ---- 取字节 ----

    /**
     * 取一条消息里带的附件字节。
     *
     * type 决定飞书按哪种资源解释 fileKey，**图片和文件不通用**：拿 file 去取图片会
     * 返回 404，而 404 在这一层是一个抛出来的错误、不是空流。
     */
    downloadResource(messageId: string, fileKey: string, type: 'image' | 'file'): Promise<Readable>;

    /**
     * 上传一张图片，拿飞书的 image_key。
     *
     * **平台没给 key 时返回 null 而不是抛**，因为这是唯一一个"失败了也能继续"的调用：
     * 上层会把这张图降级成一句文字，整条消息照发。真正的异常（网络断、鉴权失败）还是
     * 抛，由上层统一降级。
     */
    uploadImage(image: Buffer): Promise<string | null>;

    // ---- 逃生口 ----

    /**
     * 打一个端口没有专门列出的开放平台端点。
     *
     * **这是逃生口，不是通用入口。** 存在的理由只有一个：卡片延时更新
     * （`/open-apis/interactive/v1/card/update`）和仅操作者可见的卡片
     * （`/open-apis/ephemeral/v1/send`）在飞书 SDK 里没有对应方法，只能打裸端点。
     * 别拿它去打 SDK 已经封好的接口 —— 那样绕过的是类型、是错误码翻译、也是这份端口
     * 本身"能对飞书做什么"的清单。
     *
     * 鉴权、域名、tenant token 的获取与缓存全部由 SDK 客户端负责，调用方只给相对路径。
     * 响应仍按 `{ code, msg, data }` 信封解开，非 0 code 照样抛。
     */
    request<T>(method: string, path: string, body: unknown): Promise<T>;
}
