// 「复读功能」。
//
// 本文件跑的是**真的规则引擎**：谓词接错不会报错，只会让赤尾在不该说话的时候开口
// （或者在该说话的时候不开口），两种都没有运行期症状。
//
// ## 并发那一组是这批迁移的重点
//
// 拆分把这条指令的并发前提改了（推导见 counter.ts 的文件头）。这里要证明两件事：
//
//   1. 计数器**原子**时，并发的两条流只让一次复读发出去、也不会把 3 跳过去；
//   2. 这条断言**真的看得见并发问题** —— 换一个读-改-写分两步的计数器进来，同一段
//      测试立刻能看到"发两遍"和"永远不发"两种症状。
//
// 第 2 条不是凑数：串行的假替身会让第 1 条对任何实现都是绿的（C4 那批吃过这个亏 ——
// 串行替身掩盖了一个"一个方向静默丢消息"的协议不匹配）。所以两种替身都在，而且用的是
// 同一段流程。

import { describe, expect, it } from 'bun:test';
import { runRulesWith, type RuleConfig } from '@inner/shared/rules';

import type { LarkEmojiCatalog, LarkEmojiRow } from '../emoji/catalog';
import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { PostContent } from '../outbound/post-content';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import type { LarkChatPermission } from '../projection/tables';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import { repeatCounterKey, type LarkRepeatCounter } from './counter';
import { repeatCommand } from './repeat';

const APP_ID = 'cli_tool';
const BOT_NAME = 'tool';
const BOT_COMMON_USER_ID = 'cu_bot_tool';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: null, commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

// ---------------------------------------------------------------------------
// 两种计数器替身：一种原子，一种读-改-写之间会让出事件循环
// ---------------------------------------------------------------------------

interface Stored {
    chatId: string;
    msg: string;
    repeatTime: number;
}

/**
 * @param interleaves 读和写之间让出事件循环 —— 也就是拆分前那种 `GET` → 进程内加一
 *                    → `SET` 的形状。真身把这三步塞进一条 Redis 命令，所以它没有这个
 *                    让出点（见 counter.ts）。
 */
function memoryCounter(interleaves: boolean): LarkRepeatCounter {
    const cells = new Map<string, Stored>();
    return {
        async bump(chatId, hash): Promise<number> {
            const key = repeatCounterKey(chatId);
            const current = cells.get(key);
            const next = current && current.msg === hash ? current.repeatTime + 1 : 1;
            if (interleaves) await Promise.resolve();
            cells.set(key, { chatId, msg: hash, repeatTime: next });
            return next;
        },
    };
}

// ---------------------------------------------------------------------------
// 固定装置
// ---------------------------------------------------------------------------

interface Sent {
    posts: { chatId: string; content: PostContent }[];
    stickers: { chatId: string; fileKey: string }[];
}

const SMILE: LarkEmojiRow = { key: 'SMILE', text: '微笑' };

interface RigOptions {
    chatType?: string;
    text?: string;
    sticker?: string;
    image?: boolean;
    mentionsBot?: boolean;
    permission?: LarkChatPermission;
    counter?: LarkRepeatCounter;
    /** 拼在复读后面的一条规则，用来看 fallthrough 有没有生效。 */
    after?: RuleConfig;
}

function eventOf(options: RigOptions): LarkMessageEvent {
    const mentionsBot = options.mentionsBot ?? false;
    const body = options.sticker
        ? { message_type: 'sticker', content: JSON.stringify({ file_key: options.sticker }) }
        : options.image
          ? { message_type: 'image', content: '{"image_key":"img_1"}' }
          : {
                message_type: 'text',
                content: JSON.stringify({
                    text: mentionsBot
                        ? `@_user_1 ${options.text ?? '哈哈哈'}`
                        : (options.text ?? '哈哈哈'),
                }),
            };

    return {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: options.chatType ?? 'group',
            create_time: '1700000000000',
            ...body,
            mentions: mentionsBot
                ? [
                      {
                          key: '@_user_1',
                          id: { union_id: 'on_bot_tool' },
                          name: 'tool-raw',
                          mentioned_type: 'bot',
                          bot_info: { app_id: APP_ID },
                      },
                  ]
                : [],
        },
    };
}

function rig(options: RigOptions = {}) {
    const sent: Sent = { posts: [], stickers: [] };
    const counter = options.counter ?? memoryCounter(false);
    const emoji: Pick<LarkEmojiCatalog, 'emojisByText'> = {
        emojisByText: async (texts) => [SMILE].filter((row) => texts.includes(row.text)),
    };

    const deps = {
        api: {
            sendPost: async (chatId: string, content: PostContent) => {
                sent.posts.push({ chatId, content });
                return {};
            },
            sendSticker: async (chatId: string, fileKey: string) => {
                sent.stickers.push({ chatId, fileKey });
                return {};
            },
        },
        emoji,
        repeatCounter: counter,
    } as unknown as LarkCommandDeps;

    /** 收一条消息，跑一遍规则序列。 */
    function receive(each: RigOptions = {}) {
        const merged = { ...options, ...each };
        const reading = readLarkMessageEvent(eventOf(merged), bots);
        if (!reading) throw new Error('fixture is not a message event');

        const recorded: LarkRecordedInbound = {
            projection: {
                commonUserId: 'cu_sender',
                commonConversationId: 'cc_1',
                commonMessageId: 'cm_1',
                commonRootMessageId: 'cm_root',
                commonReplyMessageId: undefined,
                mentionedCommonUserIds: (merged.mentionsBot ?? false) ? [BOT_COMMON_USER_ID] : [],
            },
            commands: {
                appId: APP_ID,
                isAdmin: false,
                permission: merged.permission ?? { open_repeat_message: true },
                groupChat: null,
            },
        };

        const context = larkCommandContext(reading, recorded, BOT_NAME);
        const chatRules: RuleConfig[] = [repeatCommand(deps)(context)];
        if (merged.after) chatRules.push(merged.after);
        return runRulesWith(
            larkRuleMessage(reading, recorded.projection, {
                botName: BOT_NAME,
                commonUserId: BOT_COMMON_USER_ID,
            }),
            { chatRules, botRole: 'utility', notBlocked: async () => true },
        );
    }

    return { sent, receive, counter };
}

/** 说 n 遍同一句话。 */
async function say(receive: (o?: RigOptions) => Promise<unknown>, times: number): Promise<void> {
    for (let i = 0; i < times; i++) await receive();
}

// ---------------------------------------------------------------------------

describe('谓词：什么时候才轮得到复读', () => {
    it('群里、没 @ bot、会话开了复读 —— 命中', async () => {
        const { receive } = rig();
        const terminal = await receive();
        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('复读功能');
    });

    // 开关默认是关的（permission_config 里没这一项的老群一律等于关）。
    it('会话没开复读 —— 不命中', async () => {
        const { receive } = rig({ permission: {} });
        expect((await receive()).kind).toBe('no_match');
    });

    it('permission_config 里明确关掉 —— 不命中', async () => {
        const { receive } = rig({ permission: { open_repeat_message: false } });
        expect((await receive()).kind).toBe('no_match');
    });

    // 私聊里复读没有意义（就他一个人在说话），而且拆分前就是 OnlyGroup。
    it('私聊 —— 不命中', async () => {
        const { receive } = rig({ chatType: 'p2p' });
        expect((await receive()).kind).toBe('no_match');
    });

    // @ 了 bot 的消息归聊天主链路，不该被复读抢走。
    it('@ 了 bot —— 不命中', async () => {
        const { receive } = rig({ mentionsBot: true });
        expect((await receive()).kind).toBe('no_match');
    });

    // fallthrough=true：复读跑完还要继续往下试。拆分前就是这样 —— 一条群消息可以既
    // 被复读、又被后面的规则处理。少了它，复读会把整条规则序列在这里截断。
    it('命中之后继续往下跑，不截断规则序列', async () => {
        const ran: string[] = [];
        const { receive, sent } = rig({
            after: {
                rules: [],
                handler: async () => {
                    ran.push('后面那条');
                },
                comment: '后面那条',
                category: 'utility',
            },
        });

        await say(receive, 3);

        // 复读发了，而且排在它后面的规则每一遍都照跑 —— 少了 fallthrough，整条规则
        // 序列会在复读这里被截断。
        expect(sent.posts).toHaveLength(1);
        expect(ran).toEqual(['后面那条', '后面那条', '后面那条']);
    });
});

describe('数到三才复读', () => {
    it('前两遍不吭声，第三遍原样复读一次', async () => {
        const { receive, sent } = rig();

        await say(receive, 2);
        expect(sent.posts).toEqual([]);

        await receive();
        expect(sent.posts).toHaveLength(1);
        expect(sent.posts[0]!.chatId).toBe('oc_1');
        expect(sent.posts[0]!.content).toEqual({ content: [[{ tag: 'text', text: '哈哈哈' }]] });
    });

    // `=== 3` 而不是 `>= 3`。改成 `>=` 的话第四遍、第五遍会一直复读下去。
    it('第四遍之后不再复读', async () => {
        const { receive, sent } = rig();

        await say(receive, 6);

        expect(sent.posts).toHaveLength(1);
    });

    it('中间换了内容就从头数', async () => {
        const { receive, sent } = rig();

        await say(receive, 2);
        await receive({ text: '别的话' });
        await receive();
        await receive();

        expect(sent.posts).toEqual([]);
    });

    it('复读出去的是渲染过的富文本 —— [微笑] 换成 emotion 节点', async () => {
        const { receive, sent } = rig({ text: '哈哈[微笑]' });

        await say(receive, 3);

        expect(sent.posts[0]!.content).toEqual({
            content: [[{ tag: 'text', text: '哈哈' }, { tag: 'emotion', emoji_type: 'SMILE' }]],
        });
    });
});

describe('表情包走自己那条路', () => {
    it('同一个表情包发三遍，复读的是表情包不是富文本', async () => {
        const { receive, sent } = rig({ sticker: 'sticker_a' });

        await say(receive, 3);

        expect(sent.posts).toEqual([]);
        expect(sent.stickers).toEqual([{ chatId: 'oc_1', fileKey: 'sticker_a' }]);
    });

    it('换了一个表情包就从头数', async () => {
        const { receive, sent } = rig({ sticker: 'sticker_a' });

        await say(receive, 2);
        await receive({ sticker: 'sticker_b' });
        await receive();

        expect(sent.stickers).toEqual([]);
    });

    // 图片既不是纯文本也不是纯表情包：两条分支都不进，连计数都不该动。动了的话，群里
    // 刷几张图就会把正在累积的文字计数冲掉。
    it('图片消息两条分支都不进，也不动计数', async () => {
        const { receive, sent } = rig();

        await say(receive, 2);
        await receive({ image: true });
        await receive();

        expect(sent.posts).toHaveLength(1);
        expect(sent.stickers).toEqual([]);
    });
});

describe('并发：拆分把规则段挪出 om_id 锁之后', () => {
    // 同一条群消息被同群几个 bot 各处理一遍（拆分前有 om_id 锁串着，现在没有了），
    // 或者同一个群里两条消息挨得很近（拆分前**也没有**串——那把锁按 om_id 分，
    // 计数器按 chat_id 分）。两种都会走到这里。
    it('原子计数器：并发两条时计数不跳号，复读恰好发一次', async () => {
        const { receive, sent } = rig({ counter: memoryCounter(false) });

        await receive(); // 计数 1
        await Promise.all([receive(), receive()]); // 2 和 3

        expect(sent.posts).toHaveLength(1);
    });

    it('原子计数器：已经数到 2 时并发两条，也只发一次', async () => {
        const { receive, sent } = rig({ counter: memoryCounter(false) });

        await say(receive, 2); // 计数 2
        await Promise.all([receive(), receive()]); // 3 和 4

        expect(sent.posts).toHaveLength(1);
    });

    // ---- 对照组：证明上面两条真的看得见并发问题 ----
    //
    // 换一个读-改-写分两步的计数器（也就是拆分前那种 `GET` + `SET`），同一段流程立刻
    // 出两种症状。这两条断言写的是**缺陷**，不是期望行为 —— 它们在这里的作用是：把
    // 真身的原子性去掉，测试必须转红。

    it('对照：读-改-写分两步时，两条流各读到 2、各写 3，复读发两遍', async () => {
        const { receive, sent } = rig({ counter: memoryCounter(true) });

        await say(receive, 2);
        await Promise.all([receive(), receive()]);

        expect(sent.posts).toHaveLength(2);
    });

    it('对照：读-改-写分两步时，两条流各读到 1、各写 2，3 永远不会被观察到', async () => {
        const { receive, sent } = rig({ counter: memoryCounter(true) });

        await receive();
        await Promise.all([receive(), receive()]);
        // 原子实现在这里已经复读过了；这一版还停在 2，再说一遍才到 3。
        expect(sent.posts).toEqual([]);

        await receive();
        expect(sent.posts).toHaveLength(1);
    });
});
