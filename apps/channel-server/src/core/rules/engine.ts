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
import type { ChatRequestPayload } from 'core/services/ai/reply';

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

// 待发 ChatTrigger 意图（决策一）。persona 文本主链路 handler 在 runRules
// 阶段不实际 publish —— 只把"该发什么"登记下来，由接线点 handlers.ts 在
// storeMessage 成功之后再发 MQ（保证下游 find_message_content 先存后查、
// 不读空走"未找到消息记录"短路）。dedupeKey 是多 bot 去重锁键（全局
// internal_message_id 口径，跨 channel 唯一），锁的获取也后移到 publish
// 紧邻处（避免拿锁后 storeMessage 失败导致锁空占 60s）。
//
// savePending（必改2）：agent_responses pending 行的落库副作用，由
// makeTextReply 构造为闭包（AgentResponse 仓储逻辑仍只在 reply.ts 一处），
// 但**不在 runRules 阶段执行**。接线点 handlers.ts 抢到去重锁后才调用它，
// 与 publish 原子相邻 —— 多 bot 同群处理同一全局 message_id 时只有抢锁的
// bot 写 pending 行，未抢锁 bot 不留永不完成的孤儿 pending 行（重排前
// setNx 在 pending save 之前、未抢锁者直接 return 不 save，故这是本次
// 重排须保持的语义）。
export interface PendingChatTrigger {
    payload: ChatRequestPayload;
    lane: string | undefined;
    dedupeKey: string;
    savePending: () => Promise<void>;
}

// handler 可选第二参（决策一）。persona handler 用 registerPendingChatTrigger
// 把待发意图回传给引擎；引擎把它折进唯一终态。其余 handler 忽略此参
// （签名向后兼容，无需改动）。这不是模块级可变 outbox —— 每次
// runRulesWith 调用一个本地 capture，并发消息互不污染（与 lastResponded
// 同构、与决策四单一终态出口同构）。
export interface RuleHandlerContext {
    registerPendingChatTrigger(p: PendingChatTrigger): void;
}

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
    // 命中 persona 文本主链路时，handler 登记的待发 ChatTrigger 意图。
    // 仅 responded 终态且 handler 主动登记才有；blocked / no_match /
    // handler_error / rule_error 一律为 undefined（绝不凭空造发送意图）。
    pendingChatTrigger?: PendingChatTrigger;
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
    // persona handler 登记的待发 ChatTrigger 意图（决策一 / 建议1）。
    // 每个 handler 执行作用域内单独捕获：进入命中 handler 前新建一个
    // 本次专属 capture + 本次专属 ctx，handler 只能写自己这次的 capture。
    // 终态只绑定「产生该终态的那个 handler」本次注册的 pending —— 不再
    // 用整个 runRulesWith 调用共享的单变量、不再有"循环结束用最新
    // pending 回填"的防御写法（避免靠后 handler 没注册时把前一个
    // handler 的 pending 错绑过去）。并发安全与单一终态出口语义不变。

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

        // 命中：执行 handler。建议1：本次 handler 专属 capture + 专属
        // ctx，handler 只能写自己这次的 pending，不污染、不被污染。
        // 退出路径 5（handler 抛异常）/ 6（handler 成功）。
        let scopedPending: PendingChatTrigger | undefined;
        const scopedCtx: RuleHandlerContext = {
            registerPendingChatTrigger: (p) => {
                scopedPending = p;
            },
        };
        try {
            await handler(message, scopedCtx);
        } catch (e) {
            // handler 抛错 = 没成功响应，绝不带回任何待发意图（即便
            // 抛错前可能登记过——失败路径不发 MQ；scopedPending 随本次
            // 作用域丢弃）。
            return {
                ...base,
                kind: 'handler_error',
                matchedRule: label,
                detail: e instanceof Error ? e.message : 'Unknown error',
                skipped,
            };
        }

        if (!fallthrough) {
            return {
                ...base,
                kind: 'responded',
                matchedRule: label,
                skipped,
                pendingChatTrigger: scopedPending,
            };
        }
        // fallthrough=true：handler 已执行（已响应），继续往下试更多规则。
        // 终态记最后一次成功响应（连同它本次注册的 pending 一起快照）——
        // 循环结束后直接用 lastResponded，不再回填任何"最新 pending"。
        lastResponded = {
            ...base,
            kind: 'responded',
            matchedRule: label,
            skipped,
            pendingChatTrigger: scopedPending,
        };
    }

    // 退出路径 7：循环走完。要么有过 fallthrough 响应（responded），要么
    // 无任何规则匹配（no_match）。两者都是明确可查终态，绝无静默 return。
    // 建议1：直接用 lastResponded（它已快照「最后一个 fallthrough 响应
    // handler」本次注册的 pending），不再回填任何"最新 pending"。
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
