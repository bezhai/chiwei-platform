// 飞书专属指令清单。规则序列的前半段，也是 Task D 的迁移账本 —— 每个槽位要么已经填上
// 本服务里的规则，要么记着还欠谁一条。
//
// ## 顺序是契约，不是排版
//
// 拼在这批指令后面的是人格聊天，而它的谓词只有 `NeedRobotMention` —— 一条 @ 赤尾的消息
// 它必然命中。所以指令必须先获得匹配机会，否则所有 @bot 的消息都会先落进聊天、指令永远
// 轮不到（channel-server 那份清单的头注释写的就是这个理由，照它来）。
//
// 清单**内部**的先后同样照抄 channel-server：`Meme` 的谓词只有 `NeedRobotMention` 加一条
// async 判定，本身就近似 catch-all，它排到那几条 `EqualText` 前面会把它们全吃掉。
//
// ## 一条指令有两个阶段，不是一个
//
//     LarkCommandDeps ──装配期一次──▶ LarkCommand ──每条消息一次──▶ RuleConfig
//
// **装配期**收长命依赖：飞书 API 客户端、存储、Redis。它们只能来自组装根 —— 客户端池
// 里每个 bot 一个 SDK 客户端、各自缓存着 tenant token，逐消息重建等于每条消息换一次
// token。所以清单是个 `(deps) => 槽位` 的 factory，而不是模块加载期就定死的常量：常量
// 形态下这些东西只能来自全局单例，而全局单例正是本服务已经否决过三次的东西（事件处理
// 表、规则序列、定时任务注册表）。
//
// **每条消息**收 `LarkCommandContext`：om_id、被 @ 的人、is_admin、会话开关。理由与形状
// 见 command-context.ts 的文件头。两个阶段不要合并 —— 合了就必须二选一地牺牲掉其中一样。
//
// ## 空槽位不进规则序列
//
// 还没搬过来的槽位不产出任何指令。今天这份清单全是空的，规则序列因而只有人格聊天一条
// —— 与拆分前一致，因为这些指令此刻仍然由 channel-server 在跑。填一个槽位就是把
// `pendingIn` 换成 `command`：清单里改一行、加一个 import，指令自己那份实现在自己的
// 文件里。要用的依赖如果这里还没有，在 LarkCommandDeps 上加一行、组装根递一行 ——
// 这正是 spec 说的"填自己那个槽、递自己那个依赖"。
//
// ## `/config` 没有槽位，这是决定不是遗漏
//
// 它是「指令处理」那个斜杠指令组里的子指令，写进 `lark_base_chat_info.gray_config`，而
// agent-service 读的是 `common_conversation.attachment_policy` —— 这条链路本来就是断的
// （spec 已知缺陷四）。bezhai 2026-08-11 拍板连指令一起删掉、不迁，所以它只出现在
// DROPPED_SLASH_COMMANDS 里。整组的其余九条照迁。

import type { DataSource } from 'typeorm';
import type { RuleConfig, RuleMessage } from '@inner/shared/rules';

import type { LarkOutboundApi } from '../outbound/lark-api';
import type { LarkStore } from '../projection/tables';
import type { LarkCommandContext } from './command-context';

// ---------------------------------------------------------------------------
// 长命依赖：装配期注入一次
// ---------------------------------------------------------------------------

/**
 * Redis 上的一个键值对。
 *
 * **只有两个动作**，因为待迁的代码只用得上这两个：复读把 `repeat_msg:{chatId}` 读出来
 * 加一改回去（channel-server 的 repeat-message.ts 就是 `get` + `setWithExpire`），meme
 * 把列表缓存十分钟。要更多能力的（Lua、pipeline）自己在这上面加一个方法，别把整个
 * Redis 客户端摊开 —— 那样端口就不再说明"我们对 Redis 做了什么"。
 */
export interface LarkCommandCache {
    get(key: string): Promise<string | null>;
    setWithExpire(key: string, value: string, seconds: number): Promise<void>;
}

/**
 * 指令层的长命依赖。**组装根一次性递进来**，见 index.ts。
 *
 * 每一项都有实际待迁代码作依据，不是"为未来准备"：
 *
 *   - `api` 十条指令里九条要回复用户，发图 / meme 还要传图取图，撤回要查消息和删消息。
 *   - `store` `/bind` `/unbind` 读写 user_group_binding 与 lark_group_member，`/session`
 *     按 om_id 查 lark_message 再查 common_message，开关复读写 permission_config。
 *   - `database` 还没有专门端口的那些表从这里自建仓储 —— lark_emoji（复读唯一的读端）
 *     是 D3 的活，往 LarkStore 上加它并不合适（那是投影的端口，描述的是"一条消息进来
 *     要读写哪些行"）。
 *   - `cache` 复读的计数器、meme 列表的缓存。
 *
 * 还缺的（打 tool-service 改图 / 分词、打 meme 服务、打 302.ai 查余额）由需要它的那批
 * 自己加一行 —— 现在把没有调用方的 HTTP 客户端先建起来，是拿一个测不到的适配器换一个
 * 不存在的问题。
 */
export interface LarkCommandDeps {
    /** 对飞书能做的全部动作。见 outbound/lark-api.ts —— 那是端口，不是那个 Deployment。 */
    api: LarkOutboundApi;
    /** 飞书那几张表 + common_* 的读写。 */
    store: LarkStore;
    /** 还没有专门端口的表从这里自建仓储。 */
    database: DataSource;
    /** Redis。 */
    cache: LarkCommandCache;
}

// ---------------------------------------------------------------------------
// 清单
// ---------------------------------------------------------------------------

/**
 * 哪一批迁移任务负责把这个槽位填上。
 *
 * - `D2` 发图与卡片回调与图片日报
 * - `D3` emoji 与复读
 * - `D4` 其余指令
 *
 * D1（入站附件管线）不碰指令，所以不在这里。全部填满之后这个类型连同 `pendingIn` 那个
 * 分支一起删 —— 它是 Task D 期间的脚手架，不是长期结构。
 */
export type LarkCommandBatch = 'D2' | 'D3' | 'D4';

/** 一条指令：拿这条消息的事实，造出它在规则引擎里的样子。 */
export type LarkCommand = (context: LarkCommandContext) => RuleConfig;

/**
 * 一条斜杠子指令的本体。
 *
 * 两个参数各管一半：`message` 是规则引擎给的渠道无关视图（子指令要从 `clearText()` 里
 * 切自己的参数），`context` 是这条消息的飞书事实（om_id / 被 @ 的人 / is_admin）。参数
 * 怎么切是 D4 的事，这里只保证两样都在手上。
 */
export type LarkSlashCommand = (
    message: RuleMessage,
    context: LarkCommandContext,
) => Promise<void>;

/**
 * 清单里的一格。`name` 是跨服务对账的键，取的是 channel-server 那份清单里同一条指令的
 * `comment`。
 *
 * 联合类型本身就是双向校验：**要么有本体、要么记着谁来填，不可能两个都有、也不可能两个
 * 都没有** —— 编译期拦住，不需要运行期再查一遍。
 */
export type LarkCommandSlot =
    /** 已经搬过来了：装配期把依赖绑上，得到这条指令。 */
    | { readonly name: string; readonly command: (deps: LarkCommandDeps) => LarkCommand }
    /** 还欠着：记着谁负责填，不参与规则序列。 */
    | { readonly name: string; readonly pendingIn: LarkCommandBatch };

/** 斜杠子指令的一格。形状与顶层同构，同样靠联合类型双向校验。 */
export type LarkSlashSlot =
    | { readonly key: string; readonly run: (deps: LarkCommandDeps) => LarkSlashCommand }
    | { readonly key: string; readonly pendingIn: 'D4' };

/** 飞书专属指令，先后即优先级。 */
export const LARK_COMMANDS: readonly LarkCommandSlot[] = [
    { name: '复读功能', pendingIn: 'D3' },
    { name: '发送余额信息', pendingIn: 'D4' },
    { name: '给用户发送帮助信息', pendingIn: 'D4' },
    { name: '撤回消息', pendingIn: 'D4' },
    { name: '生成水群历史卡片', pendingIn: 'D4' },
    { name: '开启复读', pendingIn: 'D3' },
    { name: '关闭复读', pendingIn: 'D3' },
    { name: '指令处理', pendingIn: 'D4' },
    { name: '发送图片', pendingIn: 'D2' },
    { name: 'Meme', pendingIn: 'D4' },
];

/**
 * 「指令处理」那个槽位背后的斜杠指令组。这一格是一条规则、九个子指令，所以子指令另立
 * 一份清单 —— 否则"少搬了一条"在顶层清单上看不出来。
 *
 * 这不是一串给测试看的名字：`larkSlashDispatch` 直接拿它编分发表，所以"清单里有、本体
 * 没接上"没法混过去。
 */
export const LARK_SLASH_COMMANDS: readonly LarkSlashSlot[] = [
    { key: 'chat_id', pendingIn: 'D4' },
    { key: 'message_id', pendingIn: 'D4' },
    { key: 'bind', pendingIn: 'D4' },
    { key: 'unbind', pendingIn: 'D4' },
    { key: 'block', pendingIn: 'D4' },
    { key: 'unblock', pendingIn: 'D4' },
    { key: 'blocklist', pendingIn: 'D4' },
    { key: 'session', pendingIn: 'D4' },
    { key: 'union_id', pendingIn: 'D4' },
];

/** 拍板删掉、不迁的子指令。理由见文件头。 */
export const DROPPED_SLASH_COMMANDS: readonly string[] = ['config'];

// ---------------------------------------------------------------------------
// 装配
// ---------------------------------------------------------------------------

/**
 * 清单里已经填好的那些指令，依赖绑上，保持清单里的先后。
 *
 * **在装配期调一次**：返回的每条指令内部已经握着依赖，之后每条消息只走 `LarkCommand`
 * 那一跳。每条消息重跑一遍这里，等于每条消息重建一次客户端池。
 */
export function larkCommands(
    deps: LarkCommandDeps,
    slots: readonly LarkCommandSlot[] = LARK_COMMANDS,
): readonly LarkCommand[] {
    return slots.flatMap((slot) => ('command' in slot ? [slot.command(deps)] : []));
}

/**
 * 把斜杠清单编成分发表，`key` → 本体。
 *
 * 三种结果，对应账本的三种状态：
 *
 *   - **一条都还没搬** → `null`。「指令处理」那一格因此也还是空的，两边同进同退。
 *   - **全搬完了** → 分发表。
 *   - **搬了一半** → 抛。这是最坏的一种，而且是静默的：没搬的那几条既不分发也不报错，
 *     敲 `/block` 的人会掉进人格聊天、看到赤尾开始闲聊。清单直接驱动分发之后，它从
 *     "线上才发现"变成装配期一声炸。
 *
 * 同一个 key 出现两次也抛 —— 后一个会静默盖掉前一个，而两条都还在账本上。
 */
export function larkSlashDispatch(
    deps: LarkCommandDeps,
    slots: readonly LarkSlashSlot[] = LARK_SLASH_COMMANDS,
): Readonly<Record<string, LarkSlashCommand>> | null {
    const duplicates = slots
        .map((slot) => slot.key)
        .filter((key, at, all) => all.indexOf(key) !== at);
    if (duplicates.length > 0) {
        throw new Error(
            `lark-service: slash command ${[...new Set(duplicates)].join(', ')} is listed twice; ` +
                'the later one would silently shadow the earlier',
        );
    }

    const pending = slots.filter((slot) => 'pendingIn' in slot).map((slot) => slot.key);
    if (pending.length === slots.length) return null;
    if (pending.length > 0) {
        throw new Error(
            `lark-service: the slash command group is half migrated — ${pending.join(', ')} ` +
                'still have no handler, so they would fall through to the persona chat instead ' +
                'of answering. Migrate the whole group or none of it.',
        );
    }

    const table: Record<string, LarkSlashCommand> = {};
    for (const slot of slots) {
        if ('run' in slot) table[slot.key] = slot.run(deps);
    }
    return table;
}
