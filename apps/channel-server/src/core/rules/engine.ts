import { replyMessage, replyTemplate } from '@lark/basic/message';
import { CommandHandler, CommandRule } from './admin/command-handler';
import { deleteBotMessage } from './admin/delete-message';
import { genHistoryCard } from './general/gen-history';
import { checkMeme, genMeme } from '@core/services/media/meme/meme';
import { changeRepeatStatus, repeatMessage } from './group/repeat-message';
import {
    EqualText,
    NeedNotRobotMention,
    NeedRobotMention,
    OnlyGroup,
    RegexpMatch,
    RuleConfig,
    TextMessageLimit,
    WhiteGroupCheck,
    IsAdmin,
    NotBlocked,
} from './rule';
import { sendPhoto } from '@core/services/media/photo/send-photo';
import { makeTextReply } from 'core/services/ai/reply';
import { sendBalance } from './admin/balance';
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { requireLarkContext, type RuleMessage } from './rule-message';

const TOOL_BOT_APPLY_URL = process.env.TOOL_BOT_APPLY_URL || '';

// ---- 决策四：单一终态出口 ----
// 每一条进入 runRules 的消息，无论走哪条退出路径，都必须收敛到一个唯一、
// 明确、可查的 RuleTerminalState：要么记"响应了什么"，要么记"为什么没响应"。
// 禁止任何无终态记录的静默 break/return —— 所有退出点都必须 return 一个
// RuleTerminalState（这是函数返回值，类型系统强制覆盖每条路径）。
export type RuleTerminalKind =
    | 'blocked' // NotBlocked 黑名单挡掉
    | 'responded' // 命中某规则、handler 成功执行
    | 'handler_error' // 命中某规则、handler 抛异常（被捕获，仍记终态）
    | 'rule_error' // 规则执行阶段本身抛异常：notBlocked 调用 / sync 谓词 / async rule
    | 'no_match'; // 走完所有规则无任何匹配（含被 channel/botRole 过滤跳过）

export interface RuleTerminalState {
    kind: RuleTerminalKind;
    channel: string;
    messageId: string; // 全局 internal_message_id
    chatId: string;
    userId: string;
    matchedRule?: string; // responded/handler_error 时命中的规则 comment
    detail?: string; // handler_error 时的错误信息 / blocked 原因
    // 走到 no_match 之前，被 channel 过滤 / botRole 不匹配 / 规则不通过而跳过
    // 的规则清单。每一条跳过都在此留痕——禁止任何静默跳过不留记录。
    skipped: string[];
}

// runRules 的可注入内核：依赖（chatRules / botRole / NotBlocked）全部从参数
// 进，不直接摸 multiBotManager / DB —— 单测纯跑、真实链路由 runRules 注入。
export interface RuleEngineDeps {
    chatRules: RuleConfig[];
    botRole: string | undefined;
    notBlocked: (m: RuleMessage) => Promise<boolean>;
}

function ruleLabel(cfg: RuleConfig, idx: number): string {
    return cfg.comment ? cfg.comment : `rule#${idx}`;
}

// 决策五：渠道声明 + channel 过滤。channels 未声明 = 默认全平台（只有真正
// 平台无关的 persona 文本主链路这样）；声明了则当前消息 channel 不在其中就
// 跳过这条指令（并入终态 skipped）。
function ruleSupportsChannel(cfg: RuleConfig, channel: string): boolean {
    if (cfg.channels === undefined) return true;
    return cfg.channels.includes(channel);
}

export async function runRulesWith(
    message: RuleMessage,
    deps: RuleEngineDeps,
): Promise<RuleTerminalState> {
    const base = {
        channel: message.channel,
        messageId: message.internalMessageId,
        chatId: message.internalChatId,
        userId: message.internalUserId,
    };
    const skipped: string[] = [];
    // fallthrough 路径下"最后一次成功响应"的本地暂存（单一终态：循环结束
    // 统一收敛）。本地变量而非模块级——并发消息互不污染。
    let lastResponded: RuleTerminalState | undefined;

    // 退出路径 1：黑名单挡掉 —— 终态 blocked。
    // 退出路径 1b：黑名单检查本身抛错/reject —— 收敛终态 rule_error，不裸逃。
    let notBlocked: boolean;
    try {
        notBlocked = await deps.notBlocked(message);
    } catch (e) {
        return {
            ...base,
            kind: 'rule_error',
            matchedRule: 'notBlocked (blacklist check)',
            detail: e instanceof Error ? e.message : 'Unknown error',
            skipped,
        };
    }
    if (!notBlocked) {
        return {
            ...base,
            kind: 'blocked',
            detail: `user ${message.internalUserId} is blacklisted`,
            skipped,
        };
    }

    for (let idx = 0; idx < deps.chatRules.length; idx++) {
        const cfg = deps.chatRules[idx]!;
        const label = ruleLabel(cfg, idx);
        const { rules, handler, fallthrough, async_rules, category } = cfg;

        // 退出路径 2：channel 过滤跳过（决策五）。不静默——并入 skipped。
        if (!ruleSupportsChannel(cfg, message.channel)) {
            skipped.push(`${label} (channel ${message.channel} not in declared channels)`);
            continue;
        }

        // 退出路径 3b：sync 谓词 / async rule 执行本身抛错/reject ——
        // 收敛终态 rule_error（指明哪条规则），不裸逃出绕过 logTerminalState。
        let syncRulesPass: boolean;
        let asyncRulesPass: boolean;
        try {
            syncRulesPass = rules.every((rule) => rule(message));
            asyncRulesPass = async_rules
                ? (await Promise.all(async_rules.map((rule) => rule(message)))).every(
                      (result) => result,
                  )
                : true;
        } catch (e) {
            return {
                ...base,
                kind: 'rule_error',
                matchedRule: `${label} (rule predicate)`,
                detail: e instanceof Error ? e.message : 'Unknown error',
                skipped,
            };
        }

        // 退出路径 3：同步/异步规则不通过 —— 跳过该规则（留痕，非静默）。
        if (!(syncRulesPass && asyncRulesPass)) {
            skipped.push(`${label} (rules not satisfied)`);
            continue;
        }

        // 退出路径 4：botRole/category 不匹配。
        if (deps.botRole && category && category !== deps.botRole) {
            if (deps.botRole === 'persona' && NeedRobotMention(message)) {
                // persona bot 被 @ 但命中 utility 规则 → 引导申请工具 bot。
                // 这是一个明确"响应了什么"的终态（发了引导消息），不是静默。
                const applyHint = TOOL_BOT_APPLY_URL
                    ? `，请点击 ${TOOL_BOT_APPLY_URL} 申请将工具人添加到群聊`
                    : '';
                try {
                    const lark = requireLarkContext(message).larkMessage;
                    replyMessage(
                        lark.messageId,
                        `工具类功能已迁移至「赤尾工具人」${applyHint}`,
                        true,
                    );
                } catch (e) {
                    // 引导消息本身失败也必须收敛到可查终态，不静默吞。
                    return {
                        ...base,
                        kind: 'handler_error',
                        matchedRule: `${label} (utility-redirect hint)`,
                        detail: e instanceof Error ? e.message : 'Unknown error',
                        skipped,
                    };
                }
                return {
                    ...base,
                    kind: 'responded',
                    matchedRule: `${label} (utility-redirect hint)`,
                    skipped,
                };
            }
            // utility bot 跳过 persona 规则 / 非 @ 的 utility：跳过（留痕）。
            skipped.push(
                `${label} (botRole=${deps.botRole} != category=${category})`,
            );
            if (!fallthrough) {
                return { ...base, kind: 'no_match', skipped };
            }
            continue;
        }

        // 命中：执行 handler。退出路径 5（handler 抛异常）/ 6（handler 成功）。
        try {
            await handler(message);
        } catch (e) {
            return {
                ...base,
                kind: 'handler_error',
                matchedRule: label,
                detail: e instanceof Error ? e.message : 'Unknown error',
                skipped,
            };
        }

        if (!fallthrough) {
            return { ...base, kind: 'responded', matchedRule: label, skipped };
        }
        // fallthrough=true：handler 已执行（已响应），继续往下试更多规则。
        // 终态记最后一次成功响应——循环结束后统一收敛（见下方 lastResponded）。
        lastResponded = { ...base, kind: 'responded', matchedRule: label, skipped };
    }

    // 退出路径 7：循环走完。要么有过 fallthrough 响应（responded），要么
    // 无任何规则匹配（no_match）。两者都是明确可查终态，绝无静默 return。
    if (lastResponded) return lastResponded;
    return { ...base, kind: 'no_match', skipped };
}

// 真实链路入口：组装依赖（multiBotManager 取 botRole、真实 NotBlocked）后
// 调 runRulesWith，并把唯一终态记录落成可查日志（决策四：禁止静默丢弃，
// 每条消息无论走哪条退出路径都有一条可查记录）。
export async function runRules(message: RuleMessage): Promise<RuleTerminalState> {
    const botRole = multiBotManager.getBotConfig(context.getBotName() || '')?.bot_role;

    const state = await runRulesWith(message, {
        chatRules,
        botRole,
        notBlocked: NotBlocked,
    });

    logTerminalState(state);
    return state;
}

function logTerminalState(s: RuleTerminalState): void {
    const head =
        `[runRules] terminal=${s.kind} channel=${s.channel} ` +
        `message=${s.messageId} chat=${s.chatId} user=${s.userId}`;
    const tail =
        (s.matchedRule ? ` matched="${s.matchedRule}"` : '') +
        (s.detail ? ` detail="${s.detail}"` : '') +
        (s.skipped.length > 0 ? ` skipped=[${s.skipped.join(' | ')}]` : '');
    if (s.kind === 'handler_error' || s.kind === 'rule_error') {
        console.error(head + tail);
    } else {
        console.info(head + tail);
    }
}

// 定义规则和对应处理逻辑。决策五范围收紧：只有 persona 文本主链路
// makeTextReply 不声明 channels（默认全平台、真正平台无关）；其余所有 chatRule
// （凡 import 飞书 SDK/card/实体或读飞书专属字段的）显式声明 channels:['lark']，
// 内部业务逻辑不重新设计，只按 RuleMessage+LarkRuleContext 接口适配，飞书逐
// 场景行为零变化。
const chatRules: RuleConfig[] = [
    {
        rules: [
            NeedNotRobotMention,
            OnlyGroup,
            WhiteGroupCheck((chatInfo) => chatInfo.permission_config?.open_repeat_message ?? false),
        ],
        handler: repeatMessage,
        fallthrough: true,
        comment: '复读功能',
        category: 'utility',
        channels: ['lark'],
    },
    {
        rules: [EqualText('余额'), TextMessageLimit, NeedRobotMention, IsAdmin],
        handler: sendBalance,
        comment: '发送余额信息',
        category: 'utility',
        channels: ['lark'],
    },
    {
        rules: [EqualText('帮助'), TextMessageLimit, NeedRobotMention],
        handler: async (message) => {
            const lark = requireLarkContext(message).larkMessage;
            replyTemplate(lark.messageId, 'ctp_AAYrltZoypBP', undefined);
        },
        comment: '给用户发送帮助信息',
        category: 'utility',
        channels: ['lark'],
    },
    {
        rules: [EqualText('撤回'), TextMessageLimit, NeedRobotMention],
        handler: deleteBotMessage,
        comment: '撤回消息',
        channels: ['lark'],
    },
    {
        rules: [EqualText('水群', '水群趋势'), TextMessageLimit, NeedRobotMention],
        handler: genHistoryCard,
        comment: '生成水群历史卡片',
        category: 'utility',
        channels: ['lark'],
    },
    {
        rules: [EqualText('开启复读'), TextMessageLimit, NeedRobotMention, OnlyGroup],
        handler: changeRepeatStatus(true),
        category: 'utility',
        comment: '开启复读',
        channels: ['lark'],
    },
    {
        rules: [EqualText('关闭复读'), TextMessageLimit, NeedRobotMention, OnlyGroup],
        handler: changeRepeatStatus(false),
        category: 'utility',
        comment: '关闭复读',
        channels: ['lark'],
    },
    {
        rules: [CommandRule, TextMessageLimit, NeedRobotMention],
        handler: CommandHandler,
        comment: '指令处理',
        category: 'utility',
        channels: ['lark'],
    },
    {
        rules: [RegexpMatch('^发图'), TextMessageLimit, NeedRobotMention],
        handler: sendPhoto,
        comment: '发送图片',
        category: 'utility',
        channels: ['lark'],
    },
    {
        rules: [NeedRobotMention],
        async_rules: [checkMeme],
        handler: genMeme,
        comment: 'Meme',
        category: 'utility',
        channels: ['lark'],
    },
    {
        // persona 文本主链路：唯一真正平台无关、默认全平台（不声明 channels）。
        rules: [NeedRobotMention],
        handler: makeTextReply,
        comment: '聊天',
        category: 'persona',
    },
];
