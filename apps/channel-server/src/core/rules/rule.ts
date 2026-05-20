import { LarkBaseChatInfo } from 'infrastructure/dal/entities';
import { UserBlacklistRepository } from '@infrastructure/dal/repositories/repositories';
import { type RuleMessage, requireLarkContext } from './rule-message';
import type { RuleHandlerContext } from './engine';

// 规则/处理器一律消费平台无关 RuleMessage（决策五）。平台无关规则
// （EqualText/RegexpMatch/OnlyGroup/文本限定/NeedRobotMention 等）直接读
// RuleMessage 的平台无关视图；飞书强绑规则（WhiteGroupCheck/IsAdmin）经
// requireLarkContext 取回 LarkRuleContext 跑不变的内部逻辑（缺 context
// fail-loud，绝不静默）。

type Rule = (message: RuleMessage) => boolean;

type AsyncRule = (message: RuleMessage) => Promise<boolean>;

// handler 第二参 ctx 可选（决策一）：persona 文本主链路用
// ctx.registerPendingChatTrigger 把待发 ChatTrigger 意图回传引擎，由接线点
// 在 storeMessage 成功后再发 MQ。其余 handler 不声明此参即可（向后兼容）。
type Handler = (
    message: RuleMessage,
    ctx?: RuleHandlerContext,
) => Promise<void>;

/** 规则分类：utility=工具功能, persona=拟人聊天 */
export type RuleCategory = 'utility' | 'persona';

// 定义规则和对应处理逻辑的结构。新增 channels 渠道声明字段（决策五范围收紧）：
//   - 不声明 = 默认全平台（只有 persona 文本主链路这样，真正平台无关）。
//   - 声明 ['lark'] = 仅飞书：runRules 按消息 channel 过滤，非飞书消息跳过
//     （并入终态记录的 skipped）。凡 import 飞书 SDK/card/实体或读飞书专属
//     字段的 chatRule 必须显式声明 channels:['lark']。
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

// 与现有 NeedRobotMention 逻辑等价：被 @bot（addressedTargetIds 含 botIdentity）
// 或私聊（isDirect）。注意：runRules 的前置总闸（AddressingPolicy.decide +
// enforceDecision）已在接线点 D 前置判定过"要不要回"，这里保留 NeedRobotMention
// 仅作为 chatRule 内部的 rule 谓词（与改造前同语义），保证飞书逐场景行为零
// 变化——尤其复读规则用 NeedNotRobotMention，依赖本谓词的取反。
//
// botIdentity 由调用方按 channel 取（飞书是 robot_union_id）；为保持 rule 谓词
// 签名（只吃 message），这里读 RuleMessage 自带的 addressedTargetIds 是否含
// 该消息所属 bot 的标识。飞书侧 addressedTargetIds 来源与 hasMention(union_id)
// 同源（见 buildLarkRuleMessage / lark-adapter）。
let botIdentityResolver: (m: RuleMessage) => string = () => '';

// 接线点注入"按当前消息所属 bot 取 botIdentity"的函数（飞书=robot_union_id）。
// 默认空串：未注入时 group 永不命中、private 仍直通（与改造前 P2P 直通一致）。
export function setBotIdentityResolver(fn: (m: RuleMessage) => string): void {
    botIdentityResolver = fn;
}

export const NeedRobotMention: Rule = (message) => {
    if (message.isDirect) return true;
    const botIdentity = botIdentityResolver(message);
    return botIdentity.length > 0 && message.addressedTargetIds.includes(botIdentity);
};

export const NeedNotRobotMention: Rule = (message) => !NeedRobotMention(message);

export const TextMessageLimit: Rule = (message) => message.isTextOnly();

export const ContainKeyword =
    (keyword: string): Rule =>
    (message) =>
        message.text().includes(keyword);

export const EqualText =
    (...texts: string[]): Rule =>
    (message) =>
        texts.some((text) => message.clearText() === text);

export const RegexpMatch =
    (pattern: string): Rule =>
    (message) => {
        try {
            return new RegExp(pattern).test(message.clearText());
        } catch {
            return false;
        }
    };

export const OnlyP2P: Rule = (message) => message.isDirect;

export const OnlyGroup: Rule = (message) => !message.isDirect;

// ---- 飞书强绑规则（经 requireLarkContext 取回 LarkRuleContext）----
// 这些 chatRule 必声明 channels:['lark']，故 RuleMessage 必带 lark
// channelContext；缺则 requireLarkContext fail-loud（绝不静默）。内部判定逻辑
// 与改造前逐字一致，只是从 message.basicChatInfo / message.senderInfo 改成
// 从 LarkRuleContext.larkMessage 上取。

export const WhiteGroupCheck =
    (checkFunc: (chatInfo: LarkBaseChatInfo) => boolean): Rule =>
    (message) => {
        const lark = requireLarkContext(message).larkMessage;
        const chatInfo = lark.basicChatInfo;
        return chatInfo ? checkFunc(chatInfo) : false;
    };

export const IsAdmin: Rule = (message) => {
    const lark = requireLarkContext(message).larkMessage;
    return lark.senderInfo?.is_admin ?? false;
};

// 异步规则：检查用户是否未被拉黑。决策四链路顺序 D：NotBlocked 改成查全局
// user ID（用 IdentityResolver.resolve 后的 internal user id）。黑名单表
// union_id 列 → 全局 ID 的数据迁移属 5d；5b 只改查询逻辑：用 RuleMessage 上
// 已是全局 ID 的 internalUserId 作为查询值（列名 5d 迁移，本步不改 schema）。
export const NotBlocked: AsyncRule = async (message) => {
    const globalUserId = message.internalUserId;
    if (!globalUserId || globalUserId === 'unknown_sender') return true;

    const blocked = await UserBlacklistRepository.findOne({
        where: { union_id: globalUserId },
    });
    return !blocked;
};
