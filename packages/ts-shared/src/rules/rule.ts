import { userBlacklistRepo } from '../persistence/repositories';
import { type RuleMessage } from './rule-message';
import type { RuleHandlerContext } from './engine';

// 规则/处理器一律消费平台无关 RuleMessage（决策五）。本文件只保留**真正平台
// 无关**的规则（EqualText/RegexpMatch/OnlyGroup/文本限定/NeedRobotMention/
// NotBlocked 等），直接读 RuleMessage 的平台无关视图。
//
// 渠道强绑的规则（读渠道专属字段、从渠道私有 store 取数据）一律留在各自渠道的
// 插件里，不属于共享包 —— 判据不是"两边都在用"，而是"这段代码需不需要知道
// 任何具体渠道的存在"。

type Rule = (message: RuleMessage) => boolean;

type AsyncRule = (message: RuleMessage) => Promise<boolean>;

// handler 第二参 ctx 可选（决策一）：persona 文本主链路用
// ctx.registerPendingChatTrigger 把待发 ChatTrigger 意图回传引擎，由接线点
// 在该渠道入站消息写入成功后再发 MQ。其余 handler 不声明此参即可。
type Handler = (message: RuleMessage, ctx?: RuleHandlerContext) => Promise<void>;

/** 规则分类：utility=工具功能, persona=拟人聊天 */
export type RuleCategory = 'utility' | 'persona';

// 定义规则和对应处理逻辑的结构。新增 channels 渠道声明字段（决策五范围收紧）：
//   - 不声明 = 默认全平台（只有 persona 文本主链路这样，真正平台无关）。
//   - 声明具体渠道 = 仅该渠道：runRules 按消息 channel 过滤，其他渠道的消息
//     跳过（并入终态记录的 skipped）。
//   注：新模型下"这条指令属于谁"优先靠 CommandRegistry.register(channel, ...)
//   表达，channels 只是遗留的兜底口径。
export interface RuleConfig {
    rules: Rule[];
    async_rules?: AsyncRule[];
    handler: Handler;
    fallthrough?: boolean;
    comment?: string;
    category?: RuleCategory;
    channels?: string[];
}

// ---- 平台无关规则（直接读 RuleMessage 平台无关视图）----

// 与现有 NeedRobotMention 逻辑等价：私聊直通，群聊必须 @ 当前 bot。区别是
// 这里完全用 common identity：botCommonUserId 是当前 bot 在 common_user 里的
// 身份，mentionedUserIds 是消息里所有可识别 mention 投影后的 common_user_id
// 列表。各渠道的裸用户 id 必须在插件层换成 common_user_id，规则层不知道它们。
export function NeedRobotMention(message: RuleMessage): boolean {
    if (message.isDirect) return true;
    return message.mentionedUserIds.includes(message.botCommonUserId);
}

export function NeedNotRobotMention(message: RuleMessage): boolean {
    return !NeedRobotMention(message);
}

export function TextMessageLimit(message: RuleMessage): boolean {
    return message.isTextOnly();
}

export function ContainKeyword(keyword: string): Rule {
    return (message) => message.text().includes(keyword);
}

export function EqualText(...texts: string[]): Rule {
    return (message) => texts.some((text) => message.clearText() === text);
}

export function RegexpMatch(pattern: string): Rule {
    return (message) => {
        try {
            return new RegExp(pattern).test(message.clearText());
        } catch {
            return false;
        }
    };
}

export function OnlyP2P(message: RuleMessage): boolean {
    return message.isDirect;
}

export function OnlyGroup(message: RuleMessage): boolean {
    return !message.isDirect;
}

// 异步规则：检查用户是否未被拉黑。黑名单表当前列名仍为 union_id，但值口径
// 已收敛为 common_user_id；列名是历史遗留，与任何渠道的 id 体系无关。
export async function NotBlocked(message: RuleMessage): Promise<boolean> {
    const globalUserId = message.commonUserId;
    if (!globalUserId || globalUserId === 'unknown_sender') return true;

    const blocked = await userBlacklistRepo().findOne({
        where: { union_id: globalUserId },
    });
    return !blocked;
}
