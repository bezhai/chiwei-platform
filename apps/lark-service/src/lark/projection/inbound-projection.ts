// 一条飞书消息进入公共层。
//
// ## 这一步在做什么
//
// 飞书只会说 `ou_xxx` / `oc_xxx` / `om_xxx`。出了本服务，没有人认识这些 id ——
// agent-service 读的是 common_user_id / common_conversation_id / common_message_id。
// 所以每条入站消息都要先在两套 id 之间建立对应，再把消息本身按两套口径各记一份。
//
//     解析好的领域对象
//        │
//        ├─ 1. 身份对应   谁在说话、说给哪个会话      → common_user / lark_user_open_id
//        │                                             common_conversation / lark_base_chat_info
//        ├─ 2. 泳道分叉   这条该由哪条泳道处理        → 不是本进程就交出去，到此为止
//        └─ 3. 落账       消息本身                    → common_message + lark_message（同事务）
//
// ## 顺序里三个不明显但要紧的点
//
// **交接在锁外执行。** 锁只覆盖 1、2、3 —— 判定要 common_conversation_id，所以判定
// 留在锁内；交出去这个动作本身不碰本进程的任何一张表，放在锁里只会白占租约。而这把
// 锁在 Redis 上、prod 与泳道进程共用同一个（见 message-lock.ts），交接一旦是同步等待
// 接收端返回的调用，接收端重走投影就会去抢同一个 om_id 的锁，两边互等到窗口超时。
//
// **身份对应在分叉之前。** 所以即使这条消息随后被交给泳道，本进程的库里也已经留下
// 了用户行和会话行。这不是疏忽：分叉要按 common_conversation_id 查绑定，不先建
// 对应就没有可查的东西。代价是 prod 会为泳道流量留下身份行 —— 那些行本来就是共享
// 的（同一个人、同一个群），不是脏数据。
//
// **落账是整条链里唯一的事务。** 身份对应的四张表各写各的，不回滚。所以"消息没落
// 成"之后，用户和会话仍然在库里，下一条消息会复用它们。
//
// ## 并发不靠锁收敛，靠自然键
//
// 按 om_id 取的那把锁只保护"同一条消息"。同一个人、同一个会话完全可以在**两条不同
// 的消息**里被并发地第一次创建 —— 两条流各读到空、各铸一个 id，公共层里就多出一个
// 没人指向的孤儿身份。所以身份与会话都走"认领"：自然键的首写者成为 canonical，认领
// 返回库里最终生效的那一个，调用方一律用返回值（见 tables.ts）。
//
// 顺序也是这件事的一部分：**先认领渠道映射（它定 canonical），再写 common_* 那一
// 行**。反过来的话中途失败会留下没人指向的 common_user；这个顺序下留下的是"映射
// 有、common 行还没建"，下一条消息照着映射补上。
//
// ## 每一处写入对应写入矩阵的哪一行
//
//   common_user            飞书投影 / 全字段 upsert channel='lark' / 每条入站的发送者 + 每个 mention
//   common_conversation    飞书投影 / insert 或 update channel='lark' / 每条入站消息
//   common_message         飞书投影 / insert user 行 / 入站（与 lark_message 同事务）
//   common_bot_presence    飞书投影 / upsert is_active=true / 每条入站消息各刷一次
//                          刷这一下是刻意的：飞书不保证入群/退群事件必达，而
//                          agent-service 拿这张表当群投递的闸门 —— presence 因为丢
//                          事件而不准，直接表现为"该投的没投"。每条消息一次幂等
//                          upsert 换掉这个失效模式。
//
// 矩阵里属于飞书、但不在本文件的两处，各有各的触发条件，跟着它们的触发点走：
//   common_message  insert assistant → 出站
//   common_agent_response            → 出站

import type { ContentItem } from '@inner/shared/channel';
import { context } from '@inner/shared/middleware';

import { LARK_CHANNEL } from '../channel';
import { chooseInboundLane } from '../ingress/lane-handoff';
import type { InboundLaneEnvelope } from '../ingress/lane-envelope';
import type { LarkEvent } from '../ingress/lark-event';
import type { LarkMessageReading } from '../message/read-message-event';
import type { LarkInboundMessage } from '../message/parse-message';
import type { LarkMention } from '../message/wire';
import { larkDownloadAllowed } from './tables';
import type {
    CommonConversationFacts,
    LarkChatPermission,
    LarkGroupChatFacts,
    LarkStore,
    LarkTables,
    LarkUserProfile,
} from './tables';

/** 这条消息在公共层的那组 id。规则和指令都只认这些。 */
export interface LarkInboundProjection {
    commonUserId: string;
    commonConversationId: string;
    commonMessageId: string;
    /** 话题串的根。引用的消息查不到映射时退回本条自己，所以永远有值。 */
    commonRootMessageId: string;
    /** 回复锚点。没有回复对象时就是没有，不凭空造。 */
    commonReplyMessageId?: string;
    /** 被 @ 的人（含我们自己的 bot）在公共层的 id，按正文里出现的顺序去重。 */
    mentionedCommonUserIds: string[];
}

/**
 * 投影**顺路读到**的、只有飞书指令层要用的那几件事。
 *
 * 为什么它跟着投影出来，而不是等指令自己去查：这三样都跟投影本来就要读的行在**同
 * 一行**上 —— `is_admin` 跟发送者的名字（lark_user）、`permission_config` 跟会话映射
 * （lark_base_chat_info）、`user_count` / `download_has_permission_setting` 跟
 * attachment_policy（lark_group_chat_info）。多带一列不多一次查询；不带的话每条指令
 * 各查一遍，而拆分前 channel-server 正是这么干的（sendPhoto 和 genMeme 各自又查了一
 * 次 lark_group_chat_info，那一行明明已经在手上）。
 *
 * 这里的类型**不是**渠道无关的，所以它不进 RuleMessage（那份契约的文件头写死了这条）。
 * 它的去处是 rules/command-context.ts —— 飞书私有、逐消息、随消息走。
 */
export interface LarkCommandFacts {
    /**
     * 收到这条消息的飞书应用。
     *
     * 事件里没带时按处理它的 bot 兜底，与建身份映射用的是**同一个值** —— 「撤回」拿它
     * 跟被回复消息的 sender.id 比（bot 发的消息那里是 app_id 不是 union_id），两处算得
     * 不一样就会去撤别人的消息。
     */
    appId: string;
    /** 发送者是不是超级管理员（lark_user.is_admin）。这一列 nullable，读不到就是不是。 */
    isAdmin: boolean;
    /**
     * 这个会话开了哪些开关。
     *
     * 读不到那一行、或者那一列是空，交出来的都是**空对象**而不是 undefined ——
     * "没配过"一律等于关，让每个指令自己写 `?.` 兜底，迟早有人漏一处。
     */
    permission: LarkChatPermission;
    /** 群资料。私聊没有这一行，如实给 null。 */
    groupChat: LarkGroupChatFacts | null;
}

export type LarkInboundOutcome =
    | { kind: 'handed-off'; lane: string }
    | ({ kind: 'recorded' } & LarkRecordedInbound);

/**
 * 锁内那一段的产出。`hand-off` 是「判完了，信封已经备好，等着在锁外投出去」——
 * 它不出这个文件，调用方看到的仍然是 LarkInboundOutcome 的 `handed-off`。
 */
type ProjectedInbound =
    | { kind: 'hand-off'; envelope: InboundLaneEnvelope }
    | ({ kind: 'recorded' } & LarkRecordedInbound);

/** 落账之后交给规则段的东西：公共层那组 id，加上指令层要用的飞书事实。 */
export interface LarkRecordedInbound {
    projection: LarkInboundProjection;
    commands: LarkCommandFacts;
}

export interface LarkInboundDeps {
    store: LarkStore;
    /** 铸一个新的公共层 id。时间有序（uuid v7），因为它同时是主键和排序依据。 */
    newCommonId: () => string;
    /** 事件里没带 app_id 时，用处理这条事件的 bot 自己的应用兜底。 */
    appIdOfBot: (botName: string) => string;
    /** 本进程所在泳道。prod 部署是 'prod'。 */
    currentLane: string;
    laneDispatchEnabled: () => Promise<boolean>;
    laneOf: (
        channel: string,
        botGlobalId: string,
        commonConversationId: string | undefined,
    ) => Promise<string>;
    handOffToLane: (envelope: InboundLaneEnvelope) => Promise<void>;
    /** 同一条 om_id 串行处理（见 message-lock.ts）。 */
    withMessageLock: <T>(omId: string, run: () => Promise<T>) => Promise<T>;
}

export async function projectLarkInbound(
    deps: LarkInboundDeps,
    reading: LarkMessageReading,
    event: LarkEvent,
): Promise<LarkInboundOutcome> {
    const projected = await deps.withMessageLock(reading.message.messageId, () =>
        projectUnderLock(deps, reading, event),
    );
    if (projected.kind !== 'hand-off') return projected;

    // 锁已经还掉了才交出去，理由见文件顶部。投递失败往上抛（fail-closed）：吞掉就是
    // 这条消息谁也没处理，而且没有任何信号 —— 飞书那侧早就应答过了，平台不会再推一次。
    const { envelope } = projected;
    await deps.handOffToLane(envelope);
    console.info(
        `[lark-projection] handed off to lane=${envelope.lane} ` +
            `event=${event.type} message=${envelope.global_message_id}`,
    );
    return { kind: 'handed-off', lane: envelope.lane };
}

/**
 * 锁内的那一段：投影、身份落账、以及"这条归谁"的判定。
 *
 * 锁住整条投影而不只是落账那一段：并发的几个 bot 必须看到同一份"这条消息已经有
 * common_message_id 了没有"，否则它们会各铸一个。判定也在里面 —— 它要
 * registerCommonIdentities 产出的 commonConversationId 才能查绑定。
 */
async function projectUnderLock(
    deps: LarkInboundDeps,
    reading: LarkMessageReading,
    event: LarkEvent,
): Promise<ProjectedInbound> {
    // 飞书的消息事件里**没有发送者的名字**，也没有群名。两者都要回查飞书侧的
    // 档案表（它们由群成员事件和定时同步维护）。查一次，身份对应和落账都用它。
    const known = await lookUpKnownFacts(deps.store, reading.message);
    const { projection, commands } = await registerCommonIdentities(deps, reading, event, known);

    const choice = await chooseInboundLane({
        // 交接来的事件已经判过一次了。落回 prod 时本进程仍然是 prod、绑定仍然指向那条
        // 泳道，所以这个标记是拦住第二次投递的唯一东西。
        handedOff: event.handedOff === true,
        dispatchEnabled: await deps.laneDispatchEnabled(),
        currentLane: deps.currentLane,
        laneOf: () => deps.laneOf(LARK_CHANNEL, event.botName, projection.commonConversationId),
    });
    if (choice.handOff) {
        return {
            kind: 'hand-off',
            envelope: {
                channel: LARK_CHANNEL,
                event_type: event.type,
                global_message_id: projection.commonMessageId,
                trace_id: context.getTraceId(),
                lane: choice.lane,
                bot_name: event.botName,
                params: event.payload,
                handed_off: true,
            },
        };
    }

    // 在场状态是旁路：agent-service 读它判断"这个 bot 还在这个会话里吗"。写不
    // 进去不该让消息丢掉。
    //
    // 这里 await（拆分前是 fire-and-forget 加 .catch）。差别只在这一条 upsert
    // 与后面那个事务是并发还是先后，最终库里的行完全一样；换来的是"这条消息
    // 处理完了"有确定的边界，测试不必靠等一个游离的 Promise。
    try {
        await deps.store.markBotPresent(projection.commonConversationId, event.botName, true);
    } catch (error) {
        console.warn('[lark-projection] failed to refresh bot presence:', error);
    }

    await recordInboundMessage(deps.store, reading, event, projection, known);
    return { kind: 'recorded', projection, commands };
}

// ---------------------------------------------------------------------------
// 1. 身份对应
// ---------------------------------------------------------------------------

/** 事件里没有、只能回查的那两件事。查不到就留空 —— 不编一个名字出来。 */
interface KnownLarkFacts {
    sender: LarkUserProfile | null;
    groupChat: LarkGroupChatFacts | null;
}

function lookUpKnownFacts(
    store: LarkTables,
    message: LarkInboundMessage,
): Promise<KnownLarkFacts> {
    return Promise.all([
        message.sender.unionId ? store.larkUserProfile(message.sender.unionId) : null,
        // 私聊没有群资料这一行，别去问。
        message.chatType !== 'p2p' ? store.larkGroupChat(message.chatId) : null,
    ]).then(([sender, groupChat]) => ({ sender, groupChat }));
}

async function registerCommonIdentities(
    deps: LarkInboundDeps,
    reading: LarkMessageReading,
    event: LarkEvent,
    known: KnownLarkFacts,
): Promise<LarkRecordedInbound> {
    const { store } = deps;
    const message = reading.message;
    const appId = message.appId || deps.appIdOfBot(event.botName);
    const openId = message.sender.openId;
    if (!openId) {
        throw new Error('lark inbound sender has no open_id; cannot map it to a common user');
    }

    const senderProfile = known.sender;
    const groupChat = known.groupChat;

    const commonUserId = await registerCommonUser(deps, {
        appId,
        openId,
        unionId: message.sender.unionId,
        displayName: senderProfile?.name,
    });

    const mentionedCommonUserIds = await registerMentionedCommonUsers(deps, appId, reading);

    const isDirect = message.chatType === 'p2p';
    const conversation = await registerCommonConversation(deps, {
        chatId: message.chatId,
        scope: reading.inbound.conversation_scope,
        facts: {
            display_name: isDirect ? senderProfile?.name : groupChat?.name,
            avatar_url: isDirect ? senderProfile?.avatar_origin : groupChat?.avatar,
            member_count: groupChat?.user_count,
            is_active: !groupChat?.is_leave,
            attachment_policy: {
                // 群没开"所有人可下载"时，下游不该去取原图。私聊没有这个限制。
                // 判断本身在 tables.ts —— 入站附件缓存拿同一个函数当 gate。
                download_allowed: larkDownloadAllowed(groupChat),
                source: LARK_CHANNEL,
            },
        },
    });

    // 已经落过账的消息复用当时铸的 id —— 这是重放能安全跑第二遍的地基。
    const existing = await store.larkMessage(message.messageId);
    const commonMessageId = existing?.common_message_id ?? deps.newCommonId();

    return {
        projection: {
            commonUserId,
            commonConversationId: conversation.commonConversationId,
            commonMessageId,
            commonRootMessageId:
                (await resolveReference(store, message.rootId, 'root', message, commonMessageId)) ??
                commonMessageId,
            commonReplyMessageId: await resolveReference(
                store,
                message.parentId,
                'parent',
                message,
                commonMessageId,
            ),
            mentionedCommonUserIds,
        },
        commands: {
            appId,
            isAdmin: senderProfile?.is_admin === true,
            permission: conversation.permission ?? {},
            groupChat,
        },
    };
}

interface LarkUserIdentity {
    appId: string;
    openId: string;
    unionId?: string;
    displayName?: string;
}

/**
 * 这个飞书用户在公共层的 id。
 *
 * ## 收敛规则
 *
 * **优先按 union_id 找**。同一个人在每个飞书应用下 open_id 都不一样，只按
 * (app_id, open_id) 找的话，这个人在第二个应用下会被当成新人，公共层里就裂成两个
 * 用户 —— 于是"赤尾记得的那个人"和"正在说话的这个人"对不上。
 *
 * ## 并发第一次创建
 *
 * 按 om_id 取的那把锁只保护"同一条消息"，同一个人完全可以在**两条不同的消息**里被
 * 并发地第一次创建。所以这里不自己判断"要不要建"，而是把候选 id 交给认领：自然键
 * 的首写者成为 canonical，认领返回库里最终生效的那一个。**一律用返回值。**
 *
 * ## 写入顺序
 *
 * 先认领渠道映射（它定 canonical），再写 common_user 那一行。反过来的话，中途失败
 * 会留下一条没人指向的 common_user；这个顺序下，中途失败留下的是"映射有、common
 * 行还没建"，下一条消息照着映射把它补上（common_user 每次都幂等重写）。
 */
async function registerCommonUser(deps: LarkInboundDeps, who: LarkUserIdentity): Promise<string> {
    const { store } = deps;
    const [byOpenId, byUnionId] = await Promise.all([
        store.larkUserByOpenId(who.appId, who.openId),
        who.unionId ? store.larkUserByUnionId(who.unionId) : null,
    ]);
    const known = byUnionId?.common_user_id ?? byOpenId?.common_user_id;

    const key = { app_id: who.appId, open_id: who.openId };
    let canonical = await store.claimCommonUserId(
        key,
        {
            union_id: who.unionId ?? byOpenId?.union_id,
            name: who.displayName ?? byOpenId?.name ?? '',
        },
        known ?? deps.newCommonId(),
    );

    // 这个人在别的飞书应用下已经有身份了，本行却指向另一个：收敛过去。目标值由
    // larkUserByUnionId 的排序定死，两个进程算出来一样、重复跑也一样，不是竞态。
    if (known && known !== canonical) {
        await store.linkLarkUser(key, known);
        canonical = known;
    }

    await store.saveCommonUser({
        common_user_id: canonical,
        channel: LARK_CHANNEL,
        display_name: who.displayName,
    });
    return canonical;
}

/**
 * 被 @ 的人也要有公共层身份 —— 规则要按 common id 判断"这条冲谁来"，消息记录里也
 * 要留下被 @ 的是谁。
 *
 * 自家 bot 走目录里那个启动时回填的 id，不在这里铸：bot 的身份是 bot_config 的
 * 事，投影层再铸一个就成了两个"赤尾"。
 */
async function registerMentionedCommonUsers(
    deps: LarkInboundDeps,
    appId: string,
    reading: LarkMessageReading,
): Promise<string[]> {
    const out: string[] = [];
    const seen = new Set<string>();

    for (const mention of reading.message.mentions) {
        const commonUserId =
            reading.mentions.byToken(mention.key)?.botCommonUserId ??
            (await registerMentionedHuman(deps, appId, mention));
        if (!seen.has(commonUserId)) {
            seen.add(commonUserId);
            out.push(commonUserId);
        }
    }
    return out;
}

async function registerMentionedHuman(
    deps: LarkInboundDeps,
    appId: string,
    mention: LarkMention,
): Promise<string> {
    const openId = mention.id.open_id;
    if (!openId) {
        // 既不是我们自己的 bot、又没有 open_id：没有任何办法把它对应到一个人。放过
        // 去的话消息记录里会留下一个指向不存在的人的引用。
        throw new Error(
            `lark mention "${mention.name}" has no open_id and is not one of our bots; ` +
                'cannot map it to a common user',
        );
    }
    return registerCommonUser(deps, {
        appId,
        openId,
        unionId: mention.id.union_id,
        displayName: mention.name,
    });
}

interface LarkConversationFacts {
    chatId: string;
    scope: string;
    facts: CommonConversationFacts;
}

/**
 * 这个飞书会话在公共层的 id。形状与 registerCommonUser 一样：认领定 canonical，
 * 公共层那一行由它派生、每条消息幂等重写。
 *
 * 每次都重写整行（而不是只 UPDATE 会变的那几项）有两个好处：一条代码路径，以及
 * "映射有、会话行没建"的中途崩溃能被下一条消息补上。写回去的 channel / scope 是同
 * 一个值 —— 一个会话不会从私聊变成群聊。
 */
async function registerCommonConversation(
    deps: LarkInboundDeps,
    chat: LarkConversationFacts,
): Promise<{ commonConversationId: string; permission?: LarkChatPermission }> {
    const { store } = deps;
    const existing = await store.larkChat(chat.chatId);
    const commonConversationId = await store.claimCommonConversationId(
        {
            chat_id: chat.chatId,
            chat_mode: chat.scope === 'direct' ? 'p2p' : 'group',
        },
        existing?.common_conversation_id ?? deps.newCommonId(),
    );

    await store.saveCommonConversation({
        common_conversation_id: commonConversationId,
        channel: LARK_CHANNEL,
        scope: chat.scope,
        ...chat.facts,
    });
    // 开关跟着这一次读一起交出去（见 LarkCommandFacts）。指令层再查一次 = 每条入站
    // 消息多一条 SQL，而这一行此刻就在手上。
    return { commonConversationId, permission: existing?.permission_config };
}

/**
 * 把回复链上的飞书消息 id 换成公共层 id。
 *
 * 引用的那条消息可能从来没被处理过（bot 当时不在群里、那条消息没入库）。这时**丢
 * 掉这条链接、继续存本条消息** —— 让整条入站因为一个链不上的引用而失败，代价大得
 * 多。但要吼一声：这条消息的回复链是残缺的，不吼就永远查不出来。
 */
async function resolveReference(
    store: LarkTables,
    omId: string | undefined,
    kind: 'root' | 'parent',
    message: LarkInboundMessage,
    selfCommonMessageId: string,
): Promise<string | undefined> {
    if (!omId) return undefined;
    if (omId === message.messageId) return selfCommonMessageId;

    const referenced = await store.larkMessage(omId);
    if (!referenced) {
        console.warn(
            `[lark-projection] dropping the ${kind} link of ${message.messageId}: ` +
                `referenced om_id=${omId} has no common_message mapping`,
        );
        return undefined;
    }
    return referenced.common_message_id;
}

// ---------------------------------------------------------------------------
// 3. 落账
// ---------------------------------------------------------------------------

/**
 * 消息本身。**两张表必须同生共死**：只写了 common_message 就是一条在公共层存在、
 * 在飞书侧无对应物的孤儿记录 —— 回复链会从它这里断掉，撤回也找不到要撤哪条。
 * 共库（决策一）就是为了让这个事务留在原地。
 */
async function recordInboundMessage(
    store: LarkStore,
    reading: LarkMessageReading,
    event: LarkEvent,
    projection: LarkInboundProjection,
    known: KnownLarkFacts,
): Promise<void> {
    const message = reading.message;

    await store.atomically(async (tx) => {
        const existing = await tx.larkMessage(message.messageId);
        if (existing && existing.common_message_id !== projection.commonMessageId) {
            // 有人在我们算完 id 之后抢先写了别的映射。继续写下去，同一条飞书消息在
            // 公共层就有两个身份。
            throw new Error(
                `lark message ${message.messageId} already maps to ` +
                    `${existing.common_message_id}, not ${projection.commonMessageId}`,
            );
        }

        await tx.insertCommonMessage({
            common_message_id: projection.commonMessageId,
            channel: LARK_CHANNEL,
            common_conversation_id: projection.commonConversationId,
            common_user_id: projection.commonUserId,
            sender_display_name: known.sender?.name,
            role: 'user',
            content: reading.inbound.content,
            content_text: summarize(reading.inbound.content),
            common_root_message_id: projection.commonRootMessageId,
            common_reply_message_id: projection.commonReplyMessageId,
            // @ 在投影时被内联回了正文（公共层的内容契约里没有 mention 这种片段），
            // 所以"点了谁的名"只在这一刻还认得出来。不落下去，下游就只能对着一段
            // 文本猜 —— agent-service 判断"群里叫的是不是我"正是靠这一列。
            mentioned_common_user_ids: projection.mentionedCommonUserIds,
            scope: reading.inbound.conversation_scope,
            message_type: message.messageType,
            bot_name: event.botName,
            event_time: message.createTime,
        });

        if (!existing) {
            try {
                await tx.insertLarkMessage({
                    om_id: message.messageId,
                    common_message_id: projection.commonMessageId,
                    chat_id: message.chatId,
                    sender_open_id: message.sender.openId,
                    sender_union_id: message.sender.unionId,
                    root_om_id: message.rootId,
                    reply_om_id: message.parentId,
                    message_type: message.messageType,
                    // 原始报文整包留下：出问题时这是唯一能复现"飞书当时到底发了什么"
                    // 的东西。
                    raw_event: event.payload,
                });
            } catch (error) {
                throw new Error(
                    `lark message ${message.messageId} mapping insert failed; ` +
                        `common_message insert rolled back: ${(error as Error).message}`,
                );
            }
        }
    });
}

/**
 * 给人看的一行摘要（消息列表、日志、后台都读它）。非文字片段折成 `[kind]`，全空
 * 就不写 —— 空串和"没有正文"在读的人眼里是两回事。
 */
function summarize(content: ContentItem[]): string | undefined {
    const text = content
        .map((item) => (item.kind === 'text' || item.kind === 'unsupported' ? item.text : `[${item.kind}]`))
        .join('')
        .trim();
    return text.length > 0 ? text : undefined;
}
