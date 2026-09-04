// 投影完成之后：这条消息命中哪条飞书指令。
//
//     RuleMessage ──▶ runRulesWith ──▶ RuleTerminalState ──▶ 一条可查日志
//
// ## 这一段不让赤尾开口
//
// 「这条消息触发一次聊天请求」这个概念已经不存在了。她不从队列拿消息 —— 每一缝直接查
// `common_message`，自己决定要不要开口（见 agent-service 的 `app/living`）。所以入站
// 这一段既不发 MQ、也不抢去重锁、也不认领消息、也不落 `common_agent_response` 的
// pending 行：它只跑指令，然后记一条终态。
//
// 一条 @ 赤尾的普通消息因此走完序列**没有任何规则接住它**，收敛成 `no_match`。这是正确
// 终态，不是漏了什么 —— 拆掉之前接住它的是序列尾巴上那条只有 `NeedRobotMention` 的
// catch-all，而那条 catch-all 的唯一作用就是发 chat.request。
//
// ## 规则序列是参数，不是进程级注册表
//
// 共享包有两个入口：`runRules` 从进程级单例 CommandRegistry 取规则，`runRulesWith`
// 的规则从参数进。这里用后者。理由不是"更好测"，是前者对本服务**没有意义**：那个
// 注册表按 channel 分发，而本服务只有一个渠道；注册又发生在 import 期（模块加载
// 副作用），谁进了模块图决定了跑哪些规则，测试之间还会累积。
//
// 代价是 `runRules` 顺手装配的两件事要自己接上：**botRole**（从 bot 目录取，决定这个
// bot 认不认某一类规则）和 **notBlocked**（黑名单）。两者都在 deps 里。
//
// ## 规则跑在落库之后（与拆分前不同，有意）
//
// 拆分前 runRules 在 storeLarkInboundMessage 之前，落库失败时用户已经看到了 utility
// 指令的回复、库里却没有这条消息。本服务的形状不同：落账在 projectLarkInbound 内部就
// 完成了，规则接在它之后，落账失败则规则根本不跑（见 receive-message.ts）。

import type { BotConfig } from '@inner/shared/entities';
import { runRulesWith } from '@inner/shared/rules';
import type { RuleConfig, RuleMessage, RuleTerminalState } from '@inner/shared/rules';

import type { LarkEvent } from '../ingress/lark-event';
import type { LarkMessageReading } from '../message/read-message-event';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext, type LarkCommandContext } from './command-context';
import type { LarkCommand } from './commands';
import { larkRuleMessage } from './rule-message';

/**
 * 飞书指令拼成本服务认的规则序列。**每条消息拼一次。**
 *
 * 为什么不是一个常量：指令的谓词和 handler 要读这条消息的飞书事实（会话开没开复读、
 * 发送者是不是管理员、被 @ 的那个人是谁），而那些事实不能进渠道无关的 RuleMessage、也
 * 不该退回进程级上下文（理由见 command-context.ts 的文件头）。于是指令是
 * `(context) => RuleConfig`，序列跟着变成 `(context) => RuleConfig[]`。长命依赖不在这
 * 一跳里 —— 它们在装配期就已经绑进 `commands` 了（见 commands.ts）。
 *
 * 序列里**只有指令**，尾巴上不再追加任何兜底。
 */
export function larkChatRules(
    commands: readonly LarkCommand[],
): (context: LarkCommandContext) => RuleConfig[] {
    return (context) => commands.map((command) => command(context));
}

export interface LarkRulesDeps {
    /**
     * 这条消息要跑哪些规则。装配期定序、逐消息成型（见 larkChatRules）。
     *
     * 收的是**这一条消息**的指令上下文，所以指令拿到的必然是它自己那份事实 —— 不是
     * 因为谁记得清 key，而是因为它压根没有别的来源。
     */
    chatRules: (context: LarkCommandContext) => RuleConfig[];
    /** 这个 bot 是人设还是工具。取不到就是不过滤。 */
    botRoleOf: (botName: string) => string | undefined;
    /** 这个 bot 在 common_user 里的身份。取不到直接抛 —— 群聊寻址全靠它。 */
    botCommonUserId: (botName: string) => string;
    notBlocked: (message: RuleMessage) => Promise<boolean>;
}

/**
 * 跑规则，记一条终态。
 *
 * 规则引擎从不向调用方抛错：所有退出路径都收敛成 RuleTerminalState，所以这里按终态
 * 记日志，不需要也不该在规则那一段套 try/catch。返回终态是给调用方看的（可观测），
 * 不是给它做决定的。
 */
export async function applyLarkRules(
    deps: LarkRulesDeps,
    reading: LarkMessageReading,
    recorded: LarkRecordedInbound,
    event: LarkEvent,
): Promise<RuleTerminalState> {
    const message = larkRuleMessage(reading, recorded.projection, {
        botName: event.botName,
        commonUserId: deps.botCommonUserId(event.botName),
    });
    // 两份视图在同一步里现造，谁也不是谁的一部分：一份**擦掉**飞书痕迹交给渠道无关的
    // 引擎，一份**保留**飞书痕迹交给飞书自己的指令（见 command-context.ts）。
    const commandContext = larkCommandContext(reading, recorded, event.botName);

    const terminal = await runRulesWith(message, {
        chatRules: deps.chatRules(commandContext),
        botRole: deps.botRoleOf(event.botName),
        notBlocked: deps.notBlocked,
    });
    logTerminalState(terminal);

    return terminal;
}

/**
 * 每条消息一条可查记录。共享包的 runRules 自带这一步，runRulesWith 不带（它是纯
 * 内核）—— 单一终态出口的价值全在"走哪条路都能查到"，所以换了入口就得自己补上。
 *
 * 一条普通 @ 消息今天记的是 `terminal=no_match`，skipped 里列着每条没命中的指令。
 * 这不是异常路径：没有指令要处理它，而她要不要回它由她自己决定，不在这条链上。
 */
function logTerminalState(state: RuleTerminalState): void {
    const head =
        `[lark-rules] terminal=${state.kind} message=${state.messageId} ` +
        `chat=${state.chatId} user=${state.userId}`;
    const tail =
        (state.matchedRule ? ` matched="${state.matchedRule}"` : '') +
        (state.detail ? ` detail="${state.detail}"` : '') +
        (state.skipped.length > 0 ? ` skipped=[${state.skipped.join(' | ')}]` : '');
    if (state.kind === 'handler_error' || state.kind === 'rule_error') {
        console.error(head + tail);
    } else {
        console.info(head + tail);
    }
}

// ---------------------------------------------------------------------------
// 真实装配
// ---------------------------------------------------------------------------
//
// 端口在上面，把 bot 目录接上去的那几行在这里 —— 与 projection/message-lock.ts 同一个
// 形状（端口 + 真身在一个模块里，注入的只有更底层的东西）。
//
// 这几行放在组装根 index.ts 里也能跑，但那里**测不到**：index.ts 一 import 就要连
// PG / Redis / MQ。而这里的错法是静默的（bot 角色接错 → 某些消息不响应，日志干净），
// 所以把它挪进来。

/** bot 目录里规则段要问的两件事。生产上是 botDirectory 本身。 */
export interface LarkBotFacts {
    getBotConfig(botName: string): BotConfig | null;
    getBotCommonUserId(botName: string): string;
}

export interface LarkRulesInfra {
    /**
     * 本进程认哪些指令，依赖已经绑上。组装根用 `larkCommands(deps)` 造它 —— 那一步
     * 是长命依赖唯一的入口（见 commands.ts）。
     */
    commands: readonly LarkCommand[];
    bots: LarkBotFacts;
    notBlocked: (message: RuleMessage) => Promise<boolean>;
}

export function assembleLarkRules(infra: LarkRulesInfra): LarkRulesDeps {
    return {
        chatRules: larkChatRules(infra.commands),
        // 这两件事拆分前由共享包的 runRules 顺手装配，换成可注入内核之后归这里。
        botRoleOf: (botName) => infra.bots.getBotConfig(botName)?.bot_role,
        botCommonUserId: (botName) => infra.bots.getBotCommonUserId(botName),
        notBlocked: infra.notBlocked,
    };
}
