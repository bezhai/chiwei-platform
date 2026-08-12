// 一条飞书消息进来之后要读写的那些行。
//
// 这是一个**端口**，不是 ORM 的包装：它列出的每个方法都对应一条语句，而每条写入
// 都能在 spec 的「common_* 写入矩阵」里找到对应的一行。生产实现是 postgres-tables.ts
// （唯一知道 TypeORM 和 SQL 的地方），测试实现是内存里的一组 Map。
//
// 绝大多数方法服务于投影（inbound-projection.ts），但端口的范围是整条入站链而不是
// 投影一步：认领消息归谁处理发生在规则之后（矩阵里的 `common_message update bot_name`
// 那一行），它同样是一条语句、同样属于这里 —— 让它自己去摸 TypeORM 才是破口。
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
    /**
     * 超级管理员。指令层的 IsAdmin 判定读它（「余额」、`/block` 那一类）。
     *
     * 这一列 nullable，而且**跟名字在同一行上** —— 投影为了拿名字本来就要读这一行，
     * 多带一列不多一次查询。少带的话指令层只能再查一次 lark_user，或者干脆不做管理员
     * 判定（表现是任何人都能敲「余额」）。
     */
    is_admin?: boolean;
}

/**
 * 一个飞书会话上开了哪些开关（lark_base_chat_info.permission_config 这团 jsonb）。
 *
 * 全部可选：这一列本身 nullable，老会话上压根没有它，而"没配过"一律等于关。
 *
 * **不含 gray_config。** 那是同一行上的另一列，写它的 `/config` 指令已经删掉了 ——
 * 它写进去的值 agent-service 根本读不到（spec 已知缺陷四）。留一个没人写的读口，
 * 下一个人会以为那条链还活着。
 */
export interface LarkChatPermission {
    allow_send_message?: boolean;
    allow_send_pixiv_image?: boolean;
    open_repeat_message?: boolean;
    allow_send_limit_photo?: boolean;
    /**
     * 按群灰度。**当前没有读取方** —— 拆分前它进 chat.request 的 is_canary，而
     * agent-service 的 ChatTrigger 上没有这个字段，在反序列化之前就被过滤掉了
     * （见 rules/chat-request.ts 的注释）。列在这里是因为库里真的存着它。
     */
    is_canary?: boolean;
}

/** lark_base_chat_info：会话与公共层会话的对应，外加这个会话开了哪些开关。 */
export interface LarkChatRow {
    chat_id: string;
    chat_mode: 'group' | 'topic' | 'p2p';
    common_conversation_id?: string;
    permission_config?: LarkChatPermission;
}

/**
 * lark_group_member：一个人在一个群里的成员身份。
 *
 * **退群不删行，只把 is_leave 打上**，所以"这个人在不在群里"是读回来之后的判断。
 */
export interface LarkGroupMemberRow {
    chat_id: string;
    union_id: string;
    is_leave?: boolean;
    is_manager?: boolean;
    is_owner?: boolean;
}

/**
 * user_group_binding：管理员用 `/bind` 把一个人绑在一个群上，他退群就自动拉回来。
 *
 * 解绑（`/unbind`）是软删 —— 行留着，只把 is_active 关掉。所以读回来必须带上这一位：
 * 把解绑过的行当成"已经绑过了"，用户会看到"已绑定"而退群时没人拉他。
 */
export interface LarkGroupBinding {
    user_union_id: string;
    chat_id: string;
    is_active: boolean;
}

/** lark_group_chat_info：群聊独有的资料。私聊没有这一行。 */
export interface LarkGroupChatFacts {
    name: string;
    avatar?: string;
    user_count: number;
    is_leave?: boolean;
    download_has_permission_setting?: string;
}

/**
 * 这个会话允不允许把消息里的附件取下来。**私聊没有群资料这一行，一律允许。**
 *
 * 只有 `all_members` 算允许 —— 飞书那一列还有 `not_anyone` / `only_manager` 之类的值，
 * 写成"不等于 not_anyone"会让只有管理员能下载的群也放行。
 *
 * 两个读它的人必须是同一份判断：投影写进 `common_conversation.attachment_policy
 * .download_allowed`（下游据此决定要不要去取原图），入站附件缓存拿它当 gate（见
 * attachments.ts）。各写一遍的话，我们会按一套口径把附件存下来、下游按另一套口径以为
 * 存不下来，而两边都不会报错。
 */
export function larkDownloadAllowed(groupChat: LarkGroupChatFacts | null): boolean {
    return groupChat ? groupChat.download_has_permission_setting === 'all_members' : true;
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

/** 这条消息归谁处理。发送者一并重写：投影写进去的是同一个值，重写只是让它收敛。 */
export interface CommonMessageClaim {
    common_message_id: string;
    bot_name: string;
    common_user_id: string;
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
     * 一个人在一个群里的成员行。不在这个群里（从来没进过）就是 null。
     *
     * **退群的人照样读得到**，靠 is_leave 区分 —— 在这里过滤掉的话，调用方分不清
     * "没这行"和"退群了"，而 `/bind` 对这两种情况要说的话不一样。
     */
    larkGroupMember(chatId: string, unionId: string): Promise<LarkGroupMemberRow | null>;

    /** 这个人在这个群上的绑定关系。从来没绑过就是 null；解绑过的行照样读得到。 */
    larkGroupBinding(chatId: string, unionId: string): Promise<LarkGroupBinding | null>;

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
    /**
     * 把这条消息记成某个 bot 的。同群多个 bot 里，只有抢到去重锁、真的要发
     * chat.request 的那个才认领（见 rules/inbound-rules.ts 的顺序）。
     *
     * **一行都没改到就抛。** 那意味着 common_message 里根本没有这条消息，而下游
     * agent-service 拿到请求后要按 message_id 回查它 —— 读空会直接走"未找到消息记录"
     * 短路。与其发一个注定失败的请求，不如在这里炸。
     */
    claimCommonMessageForBot(claim: CommonMessageClaim): Promise<void>;

    /**
     * 建一条新的绑定，建出来就是生效的。
     *
     * **不是 upsert**：`(user_union_id, chat_id)` 上没有唯一约束，写 ON CONFLICT 会被
     * PG 直接拒绝（"没有匹配的唯一索引"）。所以判重靠调用方先 larkGroupBinding 读一
     * 次，两个管理员同时敲 `/bind` 会留下两行 —— 既有形态，登记在实体的注释里。
     */
    insertLarkGroupBinding(chatId: string, unionId: string): Promise<void>;

    /**
     * 把已有的绑定打开或关掉。
     *
     * `/unbind` 走这里而不是删行：历史上绑过谁是要留痕的，而且下次 `/bind` 同一个人
     * 时复用这一行，不会越积越多。
     */
    setLarkGroupBindingActive(chatId: string, unionId: string, isActive: boolean): Promise<void>;

    /**
     * 拨这个会话上的开关，**合并不覆盖**。
     *
     * 收的是一个 patch 而不是整份 permission_config：那一列是一团 jsonb，上面同时住着
     * 好几个互不相干的开关（见 LarkChatPermission），整份写回去就是拿一个只知道自己那
     * 一项的调用方去决定别人的值。合并语义（`jsonb ||`）定在真身里，只写一遍。
     *
     * **没有这一行时是静默的 no-op**，与拆分前一致：那条 UPDATE 匹配不到行就 0 行受
     * 影响。实际上走不到 —— 能敲出这条指令说明这个会话的消息已经投影过、行早就建好了。
     */
    setLarkChatPermission(chatId: string, patch: Partial<LarkChatPermission>): Promise<void>;

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
