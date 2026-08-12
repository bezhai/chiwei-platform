// 一条飞书消息在**指令层**的样子。
//
//     解析产出（reading）  ──┐
//     投影产出（projection）─┼──▶ LarkCommandContext ──▶ 每条指令造出自己那条 RuleConfig
//     投影顺路读到（commands）┘
//
// ## 为什么这个东西必须存在
//
// 规则引擎吃的是 RuleMessage —— 渠道无关，只有 common_* id 和几个文本问题。指令不是：
// 「撤回」要 om_id 和 parent_id，`/bind` 要被 @ 的那个人的 union_id，发图要看会话开关，
// 「余额」要看发送者是不是超级管理员。这些事实**没有一条**能进 RuleMessage。
//
// 三条别的路都走不通，各有各的坏法：
//
//   * **塞进 RuleMessage**。那份契约的文件头写死了"渠道原生的会话元信息、权限配置、
//     渠道裸 message id 绝不进这个契约，也绝不旁挂任何渠道原始对象"。留一个逃生口，
//     规则层就会慢慢长回渠道耦合 —— 而这次拆分正是在还那笔债。
//   * **各自再查一次库**。is_admin 跟发送者的名字同一行、permission_config 跟会话映射
//     同一行、群资料跟 attachment_policy 同一行，投影为了别的事已经把这三行读出来了
//     （见 projection/inbound-projection.ts 的 LarkCommandFacts）。再查一次等于把
//     "搭车读省一次查询"这个设计原样抵消掉，而且是每条指令各抵消一次。
//   * **进程级上下文**。拆分前就是这么干的：一个模块级 `Map<[botName, commonMessageId],
//     Message>`，adapter 按 key put、handler 按同一个 key get。本服务已经把同形状的东西
//     否决过两次（ingress/lark-event.ts 的事件处理表、rules/inbound-rules.ts 的规则
//     序列），理由一样：谁往里塞了什么只有运行期才知道、测试之间会互相污染、类型退化。
//
// 于是剩下的唯一一条路：**飞书私有、逐消息、随消息走的一个普通对象**，由跑规则那一步
// 现造，交给每条指令的构造函数。指令拿到的必然是"这一条消息"的事实 —— 不是因为谁记得
// 清 key，而是因为它压根没有别的来源。
//
// ## 长命依赖不在这里
//
// 飞书 API 客户端、存储、Redis 这些是**进程级注入一次**的东西，走 commands.ts 的
// LarkCommandDeps。两者不要混：混了之后要么依赖被迫逐消息重建（丢掉客户端池），要么
// 事实被迫进程级持有（回到上面那条被否决的路）。

import type { LarkContentPart } from '../message/lark-content';
import type { LarkMentionIndex } from '../message/mentions';
import type { LarkInboundMessage } from '../message/parse-message';
import type { LarkMessageReading } from '../message/read-message-event';
import type {
    LarkInboundProjection,
    LarkRecordedInbound,
} from '../projection/inbound-projection';
import type { LarkChatPermission, LarkGroupChatFacts } from '../projection/tables';

export interface LarkCommandContext {
    /**
     * 飞书说了什么。om_id / oc_id / parent_id / root_id / 发送者的 open_id 与 union_id /
     * 被 @ 的人的裸 id 全在这里 —— 指令层是飞书私有的，它认识这些。
     */
    message: LarkInboundMessage;

    /**
     * 被 @ 的人里哪几个是我们自己的 bot。
     *
     * `/bind` 一族要的是"第一个被 @ 的**真人**"，光有 message.mentions 分不出来；这个
     * 判断解析层已经做过（见 message/mentions.ts 那段"两个都要问过"），不该再做一遍。
     */
    mentions: LarkMentionIndex;

    /**
     * 正文片段，**@ 是独立的一段**（见 message/lark-content.ts）。
     *
     * RuleMessage 上那几个文本访问器（clearText / text）建在同一份片段上，但它们都已经
     * 把 @ 拍平成字了。复读要的正相反：它得把被 @ 的人重新写成飞书认的 `<at user_id=…>`
     * 标签，所以必须拿到 @ 还没被拍平的形态（见 ../repeat/echo.ts）。
     */
    content: LarkContentPart[];

    /** 这条消息在公共层的那组 id。`/session` 按它查台账，落库也按它。 */
    projection: LarkInboundProjection;

    /**
     * 当前处理这条消息的 bot。
     *
     * 同一条群消息会被同群的几个 bot 各处理一遍，所以它是**逐消息**的事实而不是进程
     * 常量。飞书客户端按它选池子（见 outbound/bot-context.ts）。
     */
    botName: string;

    /** 收到这条消息的飞书应用。「撤回」拿它跟被回复消息的 sender.id 比。 */
    appId: string;

    /** 发送者是不是超级管理员。「余额」、`/block` 一族的准入。 */
    isAdmin: boolean;

    /** 这个会话开了哪些开关。没配过一律等于关，所以永远是个对象，不会是 undefined。 */
    permission: LarkChatPermission;

    /** 群资料。私聊没有这一行。发图看 user_count，meme 看 download 权限。 */
    groupChat: LarkGroupChatFacts | null;
}

/**
 * 把这一条消息的三个来源合成指令层的视图。
 *
 * 形状与 rule-message.ts 的 `larkRuleMessage` 对偶：同样是"解析 + 投影 + 当前 bot"，
 * 只是那一份要**擦掉**飞书痕迹交给渠道无关的引擎，这一份要**保留**飞书痕迹交给飞书
 * 自己的指令。两个函数在同一步里被调用，谁也不是谁的一部分。
 */
export function larkCommandContext(
    reading: LarkMessageReading,
    recorded: LarkRecordedInbound,
    botName: string,
): LarkCommandContext {
    return {
        message: reading.message,
        mentions: reading.mentions,
        content: reading.content,
        projection: recorded.projection,
        botName,
        ...recorded.commands,
    };
}
