// RuleMessage —— runRules 消费的渠道无关统一视图。
//
// 一条消息进 runRules 前由各 channel adapter 派生成 RuleMessage。这里的字段
// （isDirect / 文本工具 / common mention list / createTime / 媒体类型判断 /
// common_*_id / channel / 当前 bot 的 common user id）足以支撑规则引擎里真正
// 渠道无关的那些规则（EqualText、RegexpMatch、OnlyGroup、文本限定等）。
//
// 渠道 ORM / SDK 强绑的东西（渠道原生的会话元信息、权限配置、发言人角色、渠道
// 裸 message id 等）**绝不进这个契约**，也**绝不旁挂任何渠道原始对象**——留一个
// "反正能从这里拿到原对象"的逃生口，规则层就会慢慢长回渠道耦合。渠道数据一律在
// 各自插件内部经该插件的私有 context store 流转：adapter 按 commonMessageId put，
// 该渠道自己的谓词 / handler 按同一个 id get —— 规则引擎永远看不到渠道对象。
//
// 各 channel 的 RuleMessage 由各自插件构造：渠道无关字段委托该渠道自己的内容
// 访问器，逐场景行为与该渠道原有实现一致。

// 渠道无关统一视图。文本/媒体工具是函数而非字段，与各 channel 的内容访问器对偶。
export interface RuleMessage {
    channel: string;
    botName: string;

    // common_* id，不是 channel 裸 ID。
    commonUserId: string;
    commonConversationId: string;
    commonMessageId: string;
    commonRootMessageId: string | undefined;

    // 派生自 InboundMessage.conversation_scope（各渠道的"单聊"取值 → isDirect）。
    isDirect: boolean;

    // 当前处理这条消息的 bot 在 common_user 里的身份。所有 channel 在进入
    // runRules 前都必须已为 bot 分配 common user id；规则层只比较 common id。
    botCommonUserId: string;

    // 消息中被提及的 common user id 列表。普通用户和已注册 bot 都必须在各自
    // channel 插件内投影成 common_user_id；规则层不接触任何渠道裸 id。
    mentionedUserIds: string[];

    // 派生自 received_at。
    createTime: number;

    // 文本工具（与各 channel 的 clearText/text/withoutEmojiText 对偶）。
    clearText(): string;
    text(): string;
    withoutEmojiText(): string;

    // 媒体类型判断（与各 channel 的 isTextOnly/isStickerOnly/stickerKey/imageKeys 对偶）。
    isTextOnly(): boolean;
    isStickerOnly(): boolean;
    stickerKey(): string;
    imageKeys(): string[];
}
