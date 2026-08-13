// 「Meme」：@ 一下赤尾，第一个词命中某个表情包模板的关键词，就现做一张发回来。
//
// 这条指令与其余九条不一样的地方，也正是要钉住的地方：
//
//   * **它的准入是一条 async 谓词**（要先问 meme 服务有哪些模板）。谓词不通过就是不命中，
//     消息继续往后走 —— 所以它虽然近似 catch-all，却不会把后面的规则吃掉。
//   * **它排在清单最后**。谓词只有 `NeedRobotMention` 加那条 async 判定，排到 `EqualText`
//     那几条前面会把它们全吃掉（清单顺序由 rules/commands.test.ts 钉）。
//   * **图片闸的判据是 `not_anyone`**，不是投影那套 `all_members`。两者不等价：只有管理员
//     能下载的群在前者眼里是**允许**的。照搬，不要顺手统一。

import { describe, expect, it } from 'bun:test';
import { Readable } from 'node:stream';
import { runRulesWith } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import type { LarkGroupChatFacts } from '../projection/tables';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import { memeCommand, parseCommandText } from './meme';
import type { LarkMeme } from './memes';

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

const TEMPLATES: LarkMeme[] = [
    { key: 'petpet', keywords: ['摸', '摸头'], params_type: { max_images: 1 } },
    { key: 'always', keywords: ['一直'], params_type: {} },
];

interface Rendered {
    name: string;
    texts: string[];
    images: number;
    args: Record<string, string>;
}

interface Did {
    rendered: Rendered[];
    downloaded: { messageId: string; fileKey: string; type: string }[];
    uploaded: Buffer[];
    images: { messageId: string; imageKey: string }[];
    replies: { messageId: string; text: string; inThread: boolean }[];
}

function rig(
    options: {
        text?: string;
        imageKeys?: string[];
        chatType?: string;
        groupChat?: LarkGroupChatFacts | null;
        templatesFail?: boolean;
        renderFails?: string;
        uploadGivesNothing?: boolean;
        templates?: LarkMeme[];
        mentionsBot?: boolean;
    } = {},
) {
    const did: Did = {
        rendered: [],
        downloaded: [],
        uploaded: [],
        images: [],
        replies: [],
    };

    const deps = {
        api: {
            downloadResource: async (messageId: string, fileKey: string, type: string) => {
                did.downloaded.push({ messageId, fileKey, type });
                return Readable.from([Buffer.from(fileKey)]);
            },
            uploadImage: async (bytes: Buffer) => {
                did.uploaded.push(bytes);
                return options.uploadGivesNothing ? null : 'img_made';
            },
            replyImage: async (messageId: string, imageKey: string) => {
                did.images.push({ messageId, imageKey });
                return {};
            },
            replyText: async (messageId: string, text: string, inThread: boolean) => {
                did.replies.push({ messageId, text, inThread });
                return {};
            },
        },
        memes: {
            templates: async () => {
                if (options.templatesFail) throw new Error('meme service is down');
                return options.templates ?? TEMPLATES;
            },
            render: async (
                name: string,
                texts: string[],
                images: Readable[],
                args: Record<string, string>,
            ) => {
                did.rendered.push({ name, texts, images: images.length, args });
                if (options.renderFails) throw new Error(options.renderFails);
                return Buffer.from('png-bytes');
            },
        },
    } as unknown as LarkCommandDeps;

    const text = options.text ?? '摸 赤尾';
    const mentionsBot = options.mentionsBot ?? true;
    const imageKeys = options.imageKeys ?? [];
    const content =
        imageKeys.length > 0
            ? JSON.stringify({
                  title: '',
                  content: [
                      [
                          { tag: 'text', text: mentionsBot ? `@_user_1 ${text}` : text },
                          ...imageKeys.map((key) => ({ tag: 'img', image_key: key })),
                      ],
                  ],
              })
            : JSON.stringify({ text: mentionsBot ? `@_user_1 ${text}` : text });

    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: options.chatType ?? 'group',
            create_time: '1700000000000',
            message_type: imageKeys.length > 0 ? 'post' : 'text',
            content,
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

    const reading = readLarkMessageEvent(event, bots);
    if (!reading) throw new Error('fixture is not a message event');

    const recorded: LarkRecordedInbound = {
        projection: {
            commonUserId: 'cu_sender',
            commonConversationId: 'cc_1',
            commonMessageId: 'cm_1',
            commonRootMessageId: 'cm_root',
            commonReplyMessageId: undefined,
            mentionedCommonUserIds: mentionsBot ? [BOT_COMMON_USER_ID] : [],
        },
        commands: {
            appId: APP_ID,
            isAdmin: false,
            permission: {},
            groupChat: options.groupChat ?? null,
        },
    };

    const context = larkCommandContext(reading, recorded, BOT_NAME);
    const message = larkRuleMessage(reading, recorded.projection, {
        botName: BOT_NAME,
        commonUserId: BOT_COMMON_USER_ID,
    });

    return {
        did,
        run: () =>
            runRulesWith(message, {
                chatRules: [memeCommand(deps)(context)],
                botRole: 'utility',
                notBlocked: async () => true,
            }),
    };
}

function group(setting?: string): LarkGroupChatFacts {
    return {
        name: '群',
        user_count: 5,
        download_has_permission_setting: setting,
    };
}

// ---------------------------------------------------------------------------

describe('切参数', () => {
    // 引号和反斜杠是拆分前就有的能力：`摸 "赤 尾"` 里那个空格属于文本，不是分隔符。
    it('空格切开，引号里的空格算文本', () => {
        expect(parseCommandText('摸 赤尾')).toEqual(['摸', '赤尾']);
        expect(parseCommandText('摸 "赤 尾"')).toEqual(['摸', '赤 尾']);
        expect(parseCommandText("摸 '赤 尾'")).toEqual(['摸', '赤 尾']);
    });

    it('反斜杠转义下一个字符', () => {
        expect(parseCommandText('摸 a\\"b')).toEqual(['摸', 'a"b']);
        expect(parseCommandText('摸 a\\ b')).toEqual(['摸', 'a b']);
    });

    it('连着的空格不产生空项', () => {
        expect(parseCommandText('摸   赤尾  ')).toEqual(['摸', '赤尾']);
        expect(parseCommandText('')).toEqual([]);
    });
});

describe('Meme：命中与生成', () => {
    it('第一个词命中关键词就现做一张发回来', async () => {
        const { did, run } = rig({ text: '摸 赤尾' });

        const terminal = await run();

        expect(terminal.kind).toBe('responded');
        expect(terminal.matchedRule).toBe('Meme');
        expect(did.rendered).toEqual([
            { name: 'petpet', texts: ['赤尾'], images: 0, args: {} },
        ]);
        expect(did.uploaded).toEqual([Buffer.from('png-bytes')]);
        expect(did.images).toEqual([{ messageId: 'om_1', imageKey: 'img_made' }]);
        expect(did.replies).toEqual([]);
    });

    // `k=v` 是参数不是文本。少了这条切分，参数会被当成一句话印在图上。
    it('带等号的词是参数，其余是文本', async () => {
        const { did, run } = rig({ text: '摸 赤尾 circle=true 好' });

        await run();

        expect(did.rendered).toEqual([
            { name: 'petpet', texts: ['赤尾', '好'], images: 0, args: { circle: 'true' } },
        ]);
    });

    it('消息里的图先取下来再交给 meme 服务', async () => {
        const { did, run } = rig({ text: '摸', imageKeys: ['img_a', 'img_b'] });

        await run();

        expect(did.downloaded).toEqual([
            { messageId: 'om_1', fileKey: 'img_a', type: 'image' },
            { messageId: 'om_1', fileKey: 'img_b', type: 'image' },
        ]);
        expect(did.rendered[0]!.images).toBe(2);
    });

    it('第一个词谁都不认识就不命中，消息交给后面的规则', async () => {
        const { did, run } = rig({ text: '你好呀' });

        const terminal = await run();

        expect(terminal.kind).toBe('no_match');
        expect(did.rendered).toEqual([]);
        expect(did.replies).toEqual([]);
    });

    // async 谓词按空格切、取第一个非空词；handler 那一侧按引号切。两套切法与拆分前
    // 逐字相同 —— 于是 `"摸" 赤尾` 这种写法谓词认不出来（第一个词是 `"摸"`）。照搬。
    it('谓词按空格切第一个词，不看引号', async () => {
        expect((await rig({ text: '"摸" 赤尾' }).run()).kind).toBe('no_match');
    });

    it('没 @ 到我不命中', async () => {
        const { did, run } = rig({ mentionsBot: false });

        expect((await run()).kind).toBe('no_match');
        expect(did.rendered).toEqual([]);
    });
});

describe('Meme：图片下载闸', () => {
    // 判据是 `not_anyone`，**不是**投影那套 `all_members`。只有管理员能下载的群
    // （`only_manager`）在这条闸眼里是允许的 —— 与拆分前逐字相同。
    it('群禁止任何人下载 + 模板吃图 + 消息真的带了图 → 拒绝', async () => {
        const { did, run } = rig({
            text: '摸',
            imageKeys: ['img_a'],
            groupChat: group('not_anyone'),
        });

        await run();

        expect(did.rendered).toEqual([]);
        expect(did.replies).toEqual([
            {
                messageId: 'om_1',
                text: '该类meme需要获取消息中图片, 但当前群聊不允许下载消息中图片, 请在其他群聊或私聊中使用',
                inThread: false,
            },
        ]);
    });

    it('只有管理员能下载的群照做（判据是 not_anyone，不是 all_members）', async () => {
        const { did, run } = rig({
            text: '摸',
            imageKeys: ['img_a'],
            groupChat: group('only_manager'),
        });

        await run();

        expect(did.rendered).toHaveLength(1);
    });

    it('禁止下载但模板不吃图 → 照做', async () => {
        const { did, run } = rig({
            text: '一直',
            imageKeys: ['img_a'],
            groupChat: group('not_anyone'),
        });

        await run();

        expect(did.rendered).toHaveLength(1);
        expect(did.rendered[0]!.name).toBe('always');
    });

    it('禁止下载但消息没带图 → 照做', async () => {
        const { did, run } = rig({ text: '摸', groupChat: group('not_anyone') });

        await run();

        expect(did.rendered).toHaveLength(1);
    });

    it('私聊没有群资料这一行，一律放行', async () => {
        const { did, run } = rig({ text: '摸', imageKeys: ['img_a'], chatType: 'p2p' });

        await run();

        expect(did.rendered).toHaveLength(1);
    });
});

describe('Meme：出错就对着用户说一句', () => {
    it('meme 服务查不到模板：谓词返回假，整条不命中', async () => {
        const { did, run } = rig({ templatesFail: true });

        expect((await run()).kind).toBe('no_match');
        expect(did.replies).toEqual([]);
    });

    it('生成失败：把 meme 服务给的说法回给用户', async () => {
        const { did, run } = rig({ text: '摸', renderFails: '文字太长了' });

        const terminal = await run();

        expect(terminal.kind).toBe('responded');
        expect(did.replies).toEqual([
            { messageId: 'om_1', text: '文字太长了', inThread: false },
        ]);
    });

    it('飞书收下了图却没给 image_key', async () => {
        const { did, run } = rig({ text: '摸', uploadGivesNothing: true });

        await run();

        expect(did.images).toEqual([]);
        expect(did.replies).toEqual([
            { messageId: 'om_1', text: '上传图片失败', inThread: false },
        ]);
    });
});
