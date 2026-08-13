// 赤尾说完一句话之后，库里要多出哪些行、以及发之前要先查哪些行。
//
// 这是一个**端口**：每个方法对应一条语句，一件事，不做判断。真身是
// postgres-tables.ts（本目录里唯一知道 TypeORM 的地方），测试用内存实现。
//
// ## 跟 projection/tables.ts 是两个端口，不是重复
//
// 两边确实都写 `common_message` 和 `lark_message`，但写的是同一张表的**两次不同
// 写入**，冲突语义正好相反：
//
//   入站  common_message(user 行) or-ignore ＋ lark_message **普通 insert，撞了要
//         回滚** —— 撞主键说明有人并发写了别的映射，留下孤儿记录比报错糟
//   出站  common_message(assistant 行) or-ignore ＋ lark_message **也 or-ignore**
//         —— 见 insertLarkMessage 的注释，出站的 om_id 会撞，而且是设计使然
//
// 把它们塞进一个端口，就得在同一个名字下并存两种冲突语义，读的人无从判断自己
// 调的是哪一种。分成两个端口之后，每个端口的每个方法只有一种语义。
//
// 字段名用**物理列名**而不是驼峰属性名，理由同 projection/tables.ts：这样写入矩阵
// 和测试断言可以逐字对上，不需要在两套命名之间来回翻译。

import type { ContentItem } from '@inner/shared/channel';

/** 赤尾发出去那条消息在公共层的样子。 */
export interface LarkAssistantMessageRow {
    common_message_id: string;
    channel: string;
    common_conversation_id: string;
    /** 说话的那个 bot 在 common_user 里的身份。 */
    common_user_id: string;
    sender_display_name?: string;
    role: string;
    content: ContentItem[];
    content_text: string;
    common_root_message_id: string;
    common_reply_message_id?: string;
    scope: string;
    message_type: string;
    bot_name: string;
    /** 毫秒时间戳的字符串形式，原样落进 bigint 列。出站写的是**发送时刻**。 */
    event_time: string;
    /** 挂回台账（common_agent_response）那一行。主动发没有台账，留空。 */
    response_id?: string;
}

/**
 * 出站消息在飞书侧的坐标。
 *
 * 只有四列 —— 出站没有 sender / root / reply / raw_event 可写：发出去这条消息的
 * 是我们自己，飞书也不会把它当作一个入站事件推回来。入站那一侧写的是另一组列，
 * 见 projection/tables.ts 的 LarkMessageRow。
 */
export interface LarkOutboundMapping {
    om_id: string;
    common_message_id: string;
    chat_id: string;
    message_type: string;
}

export interface LarkOutboundTables {
    /**
     * 公共层会话 id → 飞书裸 chat_id。没有映射返回 null。
     *
     * 主动发只走这一条反查：它没有来源消息，只能拿真实会话 id 解析投递地址。
     */
    chatIdOf(commonConversationId: string): Promise<string | null>;

    /** 公共层消息 id → 飞书裸 om_id。没有映射返回 null。 */
    omIdOf(commonMessageId: string): Promise<string | null>;

    /**
     * 飞书裸 om_id → 已经给它铸过的公共层消息 id。
     *
     * 重投时用：同一个 om_id 已经落过库就复用那个 id，别铸第二个。
     */
    commonMessageIdOf(omId: string): Promise<string | null>;

    /** insert-or-ignore。重投同一条出站消息时必须是静默 no-op。 */
    insertCommonMessage(row: LarkAssistantMessageRow): Promise<void>;

    /**
     * insert-or-ignore。
     *
     * **这一条跟入站那条不一样，撞了要忽略而不是回滚**，因为出站的 om_id 真的会撞：
     * 飞书偶尔返回 code=0 但不带 message_id，这时候落库用的是一个合成的假 id
     * （见 deliver.ts），而主动发场景下那个假 id 对每条消息都长得一样。撞了回滚的
     * 后果是整条回复的落库全丢，而消息已经真的发出去了。
     */
    insertLarkMessage(row: LarkOutboundMapping): Promise<void>;
}

export interface LarkOutboundStore extends LarkOutboundTables {
    /**
     * 一组写入要么都成、要么都不成。传给 run 的 tables 走同一条连接。
     *
     * 存在的唯一理由是 assistant 行与 lark_message 那一对：只写了前者就是一条公共层
     * 有、飞书侧无对应物的孤儿记录，之后任何按 om_id 反查它的路径（撤回、引用回复）
     * 都会读空。写入矩阵里这是唯一一处"common_* 与渠道私有表同事务"，共库决策
     * （spec 决策一）保的就是它。
     */
    atomically<T>(run: (tables: LarkOutboundTables) => Promise<T>): Promise<T>;
}
