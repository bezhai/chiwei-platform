// RuleMessage —— runRules 消费的纯平台无关统一视图（决策五 / B2）。
//
// 一条消息进 runRules 前由各 channel adapter 派生成 RuleMessage：平台无关部分
// （is_direct / 文本工具 / common mention list / createTime / 媒体类型判断 / 全局
// common_*_id / channel / 当前 bot common user id）足以支撑 runRules 里真正平台
// 无关的规则（persona 文本主链路 makeTextReply、EqualText、RegexpMatch、OnlyGroup、
// 文本限定等）。
//
// 飞书 ORM/SDK 强绑的东西（basicChatInfo / permission_config / senderInfo.is_admin
// / 原始 message_id 等）**绝不进这个契约**，也**不再旁挂任何飞书
// 对象**（B2 杀掉了 #228 的 larkMessage 逃生口）。飞书数据全部在 lark 插件内部
// 经私有 context store（plugins/lark/lark-context-store.ts）流转：lark adapter
// put、lark 谓词/handler 按全局 commonMessageId get —— core 永远看不到飞书对象。
//
// 各 channel 的 RuleMessage 由各自插件构造（飞书侧见
// plugins/lark/build-rule-message.ts，平台无关字段委托 Message 等价方法，飞书
// 逐场景行为零变化；QQ 等新 channel 按自身内容构造）。

// 平台无关统一视图。文本/媒体工具是函数而非字段，与各 channel 内容访问器对偶
// （飞书侧委托 Message，QQ 等新 channel 按自身内容构造）。
export interface RuleMessage {
    channel: string;
    botName: string;

    // common_* id，不是 channel 裸 ID。
    commonUserId: string;
    commonConversationId: string;
    commonMessageId: string;
    commonRootMessageId: string | undefined;

    // 派生自 InboundMessage.conversation_scope（飞书 p2p → direct → isDirect）。
    isDirect: boolean;

    // 当前处理这条消息的 bot 在 common_user 里的身份。所有 channel 在进入
    // runRules 前都必须已为 bot 分配 common user id；core 只比较 common id。
    botCommonUserId: string;

    // 消息中被提及的 common user id 列表。普通用户和已注册 bot 都必须在
    // channel 插件内投影到 common_user_id；core 不接触 open_id / union_id / appid。
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
