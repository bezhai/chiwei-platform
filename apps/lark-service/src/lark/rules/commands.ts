// 飞书专属指令清单。规则序列的前半段：十条顶层指令，其中「指令处理」那一格背后还有九条
// 斜杠子指令。
//
// ## 顺序是契约，不是排版
//
// 拼在这批指令后面的是人格聊天，而它的谓词只有 `NeedRobotMention` —— 一条 @ 赤尾的消息
// 它必然命中。所以指令必须先获得匹配机会，否则所有 @bot 的消息都会先落进聊天、指令永远
// 轮不到（拆分前 channel-server 那份清单的头注释写的就是这个理由，照它来）。
//
// 清单**内部**的先后同样照抄拆分前那份：`Meme` 的谓词只有 `NeedRobotMention` 加一条
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
// ## `/config` 没有槽位，这是决定不是遗漏
//
// 它是「指令处理」那个斜杠指令组里的子指令，写进 `lark_base_chat_info.gray_config`，而
// agent-service 读的是 `common_conversation.attachment_policy` —— 这条链路本来就是断的
// （spec 已知缺陷四）。bezhai 2026-08-11 拍板连指令一起删掉、不迁，所以它只出现在
// DROPPED_SLASH_COMMANDS 里。整组的其余九条照迁。

import type { DataSource } from 'typeorm';
import type { RuleConfig, RuleMessage } from '@inner/shared/rules';

import type { LarkAiProviderAccount } from '../commands/ai-provider';
import { balanceCommand } from '../commands/balance';
import { deleteBotMessageCommand } from '../commands/delete-bot-message';
import { helpCommand } from '../commands/help';
import { historyCardCommand } from '../commands/history-card';
import { memeCommand } from '../commands/meme';
import type { LarkMemes } from '../commands/memes';
import { slashCommand } from '../commands/slash';
import { bindCommand, unbindCommand } from '../commands/slash-bind';
import {
    blockCommand,
    blocklistCommand,
    unblockCommand,
} from '../commands/slash-block';
import {
    chatIdCommand,
    messageIdCommand,
    unionIdCommand,
} from '../commands/slash-ids';
import { sessionCommand } from '../commands/slash-session';
import type { LarkEmojiCatalog } from '../emoji/catalog';
import type { LarkOutboundApi } from '../outbound/lark-api';
import type { LarkReadyPhotos } from '../photo/ready';
import { sendPhotoCommand } from '../photo/send-photo';
import type { LarkKeywordExtractor } from '../commands/word-cloud';
import type { LarkStore } from '../projection/tables';
import type { LarkRepeatCounter } from '../repeat/counter';
import { repeatCommand } from '../repeat/repeat';
import { closeRepeatCommand, openRepeatCommand } from '../repeat/toggle';
import type { LarkCommandContext } from './command-context';

// ---------------------------------------------------------------------------
// 长命依赖：装配期注入一次
// ---------------------------------------------------------------------------

/**
 * Redis 上的一个键值对。**读一个、写一个带过期的**，仅此而已。
 *
 * 剩下的用户是 meme（把列表缓存十分钟，D4）。复读原本也打算走这里，后来没有 —— 它那
 * 套读-改-写必须原子，而"读一个 + 写一个"这两个动作在端口层面就表达不了原子性（推导
 * 见 ../repeat/counter.ts）。这正是这个端口该有的样子：装不下的东西自己另立一个说得清
 * 自己在做什么的端口，而不是把整个 Redis 客户端摊开。
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
 *   - `emoji` 复读要把用户原话里的 `[微笑]` 换回飞书的表情 key。它单独一个端口而不是
 *     进 LarkStore：那是投影的端口，描述的是"一条消息进来要读写哪些行"，而 lark_emoji
 *     的两个动作一个来自定时任务、一个来自指令，都不在那条链上（见 ../emoji/catalog.ts）。
 *   - `repeatCounter` 复读的"连着第几次"。它也不进 `cache` —— 那套读-改-写必须原子，
 *     而"读一个 + 写一个"在端口层面表达不了原子性（见 ../repeat/counter.ts）。
 *   - `database` 还没有专门端口的那些表从这里自建仓储。
 *   - `cache` meme 模板列表的十分钟缓存（键名跨服务共享，见 ../commands/memes.ts）。
 *   - `aiProvider` 「余额」问 302.ai 的账户情况。
 *   - `keywords` 「水群」的词云要 tool-service 分词。
 *   - `memes` 「Meme」问表情包服务有哪些模板、并让它现做一张。
 *
 * 每一项都是**先有调用方再加的**：没有调用方的 HTTP 客户端先建起来，是拿一个测不到的
 * 适配器换一个不存在的问题。
 */
export interface LarkCommandDeps {
    /** 对飞书能做的全部动作。见 outbound/lark-api.ts —— 那是端口，不是那个 Deployment。 */
    api: LarkOutboundApi;
    /** 飞书那几张表 + common_* 的读写。 */
    store: LarkStore;
    /** lark_emoji。写端是 emoji-sync 定时任务，读端只有复读。 */
    emoji: LarkEmojiCatalog;
    /** 复读的计数器。**必须原子** —— 理由见 ../repeat/counter.ts。 */
    repeatCounter: LarkRepeatCounter;
    /** 还没有专门端口的表从这里自建仓储。 */
    database: DataSource;
    /** Redis。 */
    cache: LarkCommandCache;
    /** 我们在 302.ai 上那个账户还剩多少钱。只有「余额」问它。 */
    aiProvider: LarkAiProviderAccount;
    /** 打 tool-service 分词。只有「水群」那张卡片上的词云用它。 */
    keywords: LarkKeywordExtractor;
    /** 表情包服务：有哪些模板、现做一张。列表那层缓存在 memes.ts 里。 */
    memes: LarkMemes;
    /**
     * 取一批**飞书发得出去**的图（每张都保证有 image_key）。
     *
     * 背后是另一个 Mongo 实例 + 对象存储 + 一次 tool-service 缩图 + 一次飞书上传，
     * 全部收在这一个函数后面 —— 指令层不该认识其中任何一样。卡片回调和定时任务用的
     * 是同一个（它们跟指令同进程），所以口径只有一份。
     */
    photos: LarkReadyPhotos;
}

// ---------------------------------------------------------------------------
// 清单
// ---------------------------------------------------------------------------

/** 一条指令：拿这条消息的事实，造出它在规则引擎里的样子。 */
export type LarkCommand = (context: LarkCommandContext) => RuleConfig;

/**
 * 一条斜杠子指令的本体。
 *
 * 两个参数各管一半：`message` 是规则引擎给的渠道无关视图（子指令要从 `clearText()` 里
 * 切自己的参数），`context` 是这条消息的飞书事实（om_id / 被 @ 的人 / is_admin）。参数
 * 怎么切是每条子指令自己的事，这一层只保证两样都在手上。
 */
export type LarkSlashCommand = (
    message: RuleMessage,
    context: LarkCommandContext,
) => Promise<void>;

/**
 * 清单里的一格：`name` 是这条指令在规则引擎里的 `comment`，`command` 在装配期把依赖绑上
 * 得到指令本体。
 */
export interface LarkCommandSlot {
    readonly name: string;
    readonly command: (deps: LarkCommandDeps) => LarkCommand;
}

/** 斜杠子指令的一格。形状与顶层同构，`key` 是 `/xxx` 里的那个 xxx。 */
export interface LarkSlashSlot {
    readonly key: string;
    readonly run: (deps: LarkCommandDeps) => LarkSlashCommand;
}

/** 飞书专属指令，先后即优先级。 */
export const LARK_COMMANDS: readonly LarkCommandSlot[] = [
    { name: '复读功能', command: repeatCommand },
    { name: '发送余额信息', command: balanceCommand },
    { name: '给用户发送帮助信息', command: helpCommand },
    { name: '撤回消息', command: deleteBotMessageCommand },
    { name: '生成水群历史卡片', command: historyCardCommand },
    { name: '开启复读', command: openRepeatCommand },
    { name: '关闭复读', command: closeRepeatCommand },
    { name: '指令处理', command: slashCommand },
    { name: '发送图片', command: sendPhotoCommand },
    { name: 'Meme', command: memeCommand },
];

/**
 * 「指令处理」那个槽位背后的斜杠指令组。这一格是一条规则、九个子指令，所以子指令另立
 * 一份清单 —— 否则少掉一条在顶层清单上看不出来。
 *
 * 这不是一串给测试看的名字：`larkSlashDispatch` 直接拿它编分发表，所以"清单里有、本体
 * 没接上"没法混过去。
 */
export const LARK_SLASH_COMMANDS: readonly LarkSlashSlot[] = [
    { key: 'chat_id', run: chatIdCommand },
    { key: 'message_id', run: messageIdCommand },
    { key: 'bind', run: bindCommand },
    { key: 'unbind', run: unbindCommand },
    { key: 'block', run: blockCommand },
    { key: 'unblock', run: unblockCommand },
    { key: 'blocklist', run: blocklistCommand },
    { key: 'session', run: sessionCommand },
    { key: 'union_id', run: unionIdCommand },
];

/** 拍板删掉、不迁的子指令。理由见文件头。 */
export const DROPPED_SLASH_COMMANDS: readonly string[] = ['config'];

// ---------------------------------------------------------------------------
// 装配
// ---------------------------------------------------------------------------

/**
 * 清单里的每条指令，依赖绑上，保持清单里的先后。
 *
 * **在装配期调一次**：返回的每条指令内部已经握着依赖，之后每条消息只走 `LarkCommand`
 * 那一跳。每条消息重跑一遍这里，等于每条消息重建一次客户端池。
 */
export function larkCommands(
    deps: LarkCommandDeps,
    slots: readonly LarkCommandSlot[] = LARK_COMMANDS,
): readonly LarkCommand[] {
    return slots.map((slot) => slot.command(deps));
}

/**
 * 把斜杠清单编成分发表，`key` → 本体。
 *
 * 同一个 key 出现两次就抛 —— 后一个会静默盖掉前一个，而两条在清单上都还在，谁也看不出
 * 少了一条。
 */
export function larkSlashDispatch(
    deps: LarkCommandDeps,
    slots: readonly LarkSlashSlot[] = LARK_SLASH_COMMANDS,
): Readonly<Record<string, LarkSlashCommand>> {
    const duplicates = slots
        .map((slot) => slot.key)
        .filter((key, at, all) => all.indexOf(key) !== at);
    if (duplicates.length > 0) {
        throw new Error(
            `lark-service: slash command ${[...new Set(duplicates)].join(', ')} is listed twice; ` +
                'the later one would silently shadow the earlier',
        );
    }

    const table: Record<string, LarkSlashCommand> = {};
    for (const slot of slots) {
        table[slot.key] = slot.run(deps);
    }
    return table;
}
