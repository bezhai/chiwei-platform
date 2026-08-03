// 投影要读写的那些行。
//
// 这是一个**端口**，不是 ORM 的包装：它列出的每个方法都对应一条语句，而每条写入
// 都能在 spec 的「common_* 写入矩阵」里找到对应的一行。生产实现是 postgres-tables.ts
// （唯一知道 TypeORM 和 SQL 的地方），测试实现是内存里的一组 Map。
//
// 字段名刻意用**物理列名**而不是驼峰属性名：这些结构描述的是"库里那一行长什么样"，
// 用列名之后写入矩阵和测试断言可以逐字对上，不需要在两套命名之间来回翻译。
//
// 为什么要有 atomically：common_message 的 user 行与 lark_message 必须同生共死 ——
// 只写了前者就是一条在公共层存在、在飞书侧无对应物的孤儿记录，回复链会从它这里
// 断掉。这是整个共库决策（决策一）唯一要保住的东西，所以它是端口的一等公民，而
// 不是某个实现的内部细节。
//
// 为什么"认领"是端口的一等公民：同一个人、同一个会话会在**不同的消息**里被并发地
// 第一次创建（按 om_id 取的那把锁只保护同一条消息）。用两次通用 upsert 在调用方
// 拼原子语义拼不出来 —— 两条流各自读到空、各铸一个 id，后写的还会覆盖前一个。所以
// 冲突契约必须由端口显式表达：**自然键的首写者成为 canonical，认领返回库里最终生效
// 的那一个**，调用方一律用返回值，不用自己铸的候选值。

import type { ContentItem } from '@inner/shared/channel';

// ---- 读回来的行 ----

/** lark_user_open_id：一个人在某个飞书应用下的身份，以及它通往公共层的桥。 */
export interface LarkUserLink {
    app_id: string;
    open_id: string;
    union_id?: string;
    name: string;
    common_user_id?: string;
}

/** 认领用的自然键。同一个人在每个飞书应用下各有一个 open_id。 */
export interface LarkUserKey {
    app_id: string;
    open_id: string;
}

/** 认领时顺带刷新的可变列。**不含 common_user_id** —— 那个只认首写者。 */
export interface LarkUserFacts {
    union_id?: string;
    name: string;
}

/** 认领会话用的自然键 + 建行时才写的 chat_mode。 */
export interface LarkChatKey {
    chat_id: string;
    chat_mode: 'group' | 'topic' | 'p2p';
}

/** lark_user：这个人在开放平台维度的档案。飞书的消息事件里**没有发送者的名字**。 */
export interface LarkUserProfile {
    name: string;
    avatar_origin?: string;
}

/** lark_base_chat_info：会话与公共层会话的对应。 */
export interface LarkChatRow {
    chat_id: string;
    chat_mode: 'group' | 'topic' | 'p2p';
    common_conversation_id?: string;
}

/** lark_group_chat_info：群聊独有的资料。私聊没有这一行。 */
export interface LarkGroupChatFacts {
    name: string;
    avatar?: string;
    user_count: number;
    is_leave?: boolean;
    download_has_permission_setting?: string;
}

/** lark_message：飞书消息与公共层消息的对应。 */
export interface LarkMessageRow {
    om_id: string;
    common_message_id: string;
    chat_id: string;
    sender_open_id?: string;
    sender_union_id?: string;
    root_om_id?: string;
    reply_om_id?: string;
    message_type: string;
    raw_event?: unknown;
}

// ---- 写进去的行 ----

export interface CommonUserRow {
    common_user_id: string;
    channel: string;
    display_name?: string;
}

/** 会话上会随时间变化的那几项。建行和改行共用同一组事实。 */
export interface CommonConversationFacts {
    display_name?: string;
    avatar_url?: string;
    member_count?: number;
    is_active: boolean;
    attachment_policy: { download_allowed: boolean; source: string };
}

export interface CommonConversationRow extends CommonConversationFacts {
    common_conversation_id: string;
    channel: string;
    /** 建行时定死，之后不再改 —— 一个会话不会从私聊变成群聊。 */
    scope: string;
}

export interface CommonMessageRow {
    common_message_id: string;
    channel: string;
    common_conversation_id: string;
    common_user_id: string;
    sender_display_name?: string;
    role: string;
    content: ContentItem[];
    content_text?: string;
    common_root_message_id: string;
    common_reply_message_id?: string;
    scope: string;
    message_type: string;
    bot_name: string;
    /** 飞书给的毫秒时间戳字符串，原样落进 bigint 列。 */
    event_time: string;
}

// ---- 端口 ----

export interface LarkTables {
    larkUserByOpenId(appId: string, openId: string): Promise<LarkUserLink | null>;
    /**
     * 按 union_id 找这个人已有的公共层身份。
     * **必须按 common_user_id 升序取第一条**：同一个 union_id 在多个飞书应用下各有
     * 一行，取哪一条要是不确定，两个进程会各选一条、把同一个人分成两半。
     */
    larkUserByUnionId(unionId: string): Promise<LarkUserLink | null>;
    larkUserProfile(unionId: string): Promise<LarkUserProfile | null>;
    larkChat(chatId: string): Promise<LarkChatRow | null>;
    larkGroupChat(chatId: string): Promise<LarkGroupChatFacts | null>;
    larkMessage(omId: string): Promise<LarkMessageRow | null>;

    /**
     * 认领这个飞书用户的公共层身份，**首写者成为 canonical**。
     *
     * 一条语句完成三件事：没有这一行就带着 candidate 插进去；已经有了就只刷可变列，
     * 并**保留**已经写在里面的 common_user_id（另一条流先到了，或者别的代码路径先
     * 建了行但没填 id）。返回的是库里最终生效的那一个 —— 可能不是 candidate。
     */
    claimCommonUserId(
        key: LarkUserKey,
        facts: LarkUserFacts,
        candidate: string,
    ): Promise<string>;
    /**
     * 把这一行改指到另一个 common_user_id。
     *
     * 只用在一处：这个人在别的飞书应用下已经有身份了，本行要收敛过去。目标值由
     * larkUserByUnionId 的排序定死，两个进程算出来一样，所以这不是竞态写入。
     */
    linkLarkUser(key: LarkUserKey, commonUserId: string): Promise<void>;
    /** 认领这个飞书会话的公共层身份。语义同 claimCommonUserId。 */
    claimCommonConversationId(chat: LarkChatKey, candidate: string): Promise<string>;

    /** upsert（主键冲突就覆盖）。值是 undefined 的列不参与覆盖。 */
    saveCommonUser(row: CommonUserRow): Promise<void>;
    /** upsert on common_conversation_id。 */
    saveCommonConversation(row: CommonConversationRow): Promise<void>;
    /**
     * insert-or-ignore。重放同一条消息时必须是静默的 no-op —— 这是整条投影能安全
     * 跑第二遍的地基。
     */
    insertCommonMessage(row: CommonMessageRow): Promise<void>;
    /**
     * 普通 insert，主键冲突就抛。调用方在同事务里先确认过没有这一行；真撞上了说明
     * 有人并发写进来了，这时候必须让整个事务回滚而不是忽略。
     */
    insertLarkMessage(row: LarkMessageRow): Promise<void>;
    /** upsert on (common_conversation_id, bot_name)。 */
    markBotPresent(
        commonConversationId: string,
        botName: string,
        isActive: boolean,
    ): Promise<void>;
}

export interface LarkStore extends LarkTables {
    /**
     * 一组写入要么都成、要么都不成。传给 run 的 tables 走同一条连接。
     *
     * 存在的唯一理由是 common_message + lark_message 那一对（见文件顶部）。别把它
     * 当成通用的"顺手包一下"—— 事务开着的时候拿着连接不放，链路里的每一步都会
     * 变成锁的持有时间。
     */
    atomically<T>(run: (tables: LarkTables) => Promise<T>): Promise<T>;
}
