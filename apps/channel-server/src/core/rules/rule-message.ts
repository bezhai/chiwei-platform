// RuleMessage —— runRules 消费的纯平台无关统一视图（决策五 / B2）。
//
// 一条消息进 runRules 前由各 channel adapter 派生成 RuleMessage：平台无关部分
// （is_direct / 文本工具 / 结构化 mentions / createTime / 媒体类型判断 / 全局
// internal_*_id / channel）足以支撑 runRules 里真正平台无关的规则（persona 文本
// 主链路 makeTextReply、EqualText、RegexpMatch、OnlyGroup、文本限定等）。
//
// 飞书 ORM/SDK 强绑的东西（basicChatInfo / permission_config / senderInfo.is_admin
// / 原始 message_id / mentionMap 等）**绝不进这个契约**，也**不再旁挂任何飞书
// 对象**（B2 杀掉了 #228 的 larkMessage 逃生口）。飞书数据全部在 lark 插件内部
// 经私有 context store（plugins/lark/lark-context-store.ts）流转：lark adapter
// put、lark 谓词/handler 按全局 internalMessageId get —— core 永远看不到飞书对象。
//
// 各 channel 的 RuleMessage 由各自插件构造（飞书侧见
// plugins/lark/build-rule-message.ts，平台无关字段委托 Message 等价方法，飞书
// 逐场景行为零变化；QQ 等新 channel 按自身内容构造）。

// 平台无关统一视图。文本/媒体工具是函数而非字段，与各 channel 内容访问器对偶
// （飞书侧委托 Message，QQ 等新 channel 按自身内容构造）。
export interface RuleMessage {
    channel: string;
    botName: string;

    // 全局 internal_*_id（IdentityResolver.resolve 之后的，不是 channel 裸 ID）。
    internalUserId: string;
    internalChatId: string;
    internalMessageId: string;
    internalRootId: string | undefined;

    // 派生自 InboundMessage.conversation_scope（飞书 p2p → direct → isDirect）。
    isDirect: boolean;

    // 结构化寻址线索（飞书是 mention 的 union_id 列表；QQ 是 at 的 appid）。
    addressedTargetIds: string[];

    // 派生自 received_at。
    createTime: number;

    // 文本工具（与各 channel 的 clearText/text/withMentionText/withoutEmojiText 对偶）。
    clearText(): string;
    text(): string;
    withMentionText(): string;
    withoutEmojiText(): string;

    // 媒体类型判断（与各 channel 的 isTextOnly/isStickerOnly/stickerKey/imageKeys 对偶）。
    isTextOnly(): boolean;
    isStickerOnly(): boolean;
    stickerKey(): string;
    imageKeys(): string[];
}
