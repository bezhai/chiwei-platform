import { describe, expect, it } from 'bun:test';
import { LarkClient } from '@inner/lark-utils';
import { Readable } from 'node:stream';

import type { PostContent } from './post-content';
import {
    createSdkLarkApi,
    larkClientPool,
    type LarkApiClient,
    type LarkClientPool,
} from './sdk-lark-api';

const post: PostContent = { content: [[{ tag: 'md', text: '你好' }]] };

interface Recorded {
    sent: Array<{ receiveId: string; content: unknown; msgType: string }>;
    replied: Array<{ messageId: string; content: unknown; msgType: string; inThread?: boolean }>;
    deleted: string[];
    uploaded: unknown[];
    fetchedMessages: string[];
    listed: unknown[];
    fetchedUsers: Array<{ userId: string; idType?: string }>;
    downloaded: Array<{ messageId: string; fileKey: string; type: string }>;
    invited: Array<{ chatId: string; memberId: string; idType?: string }>;
    requested: Array<{ url: string; data: unknown; method: string }>;
}

function fakeClient(over: Partial<LarkApiClient> = {}): { client: LarkApiClient } & Recorded {
    const recorded: Recorded = {
        sent: [],
        replied: [],
        deleted: [],
        uploaded: [],
        fetchedMessages: [],
        listed: [],
        fetchedUsers: [],
        downloaded: [],
        invited: [],
        requested: [],
    };
    const client: LarkApiClient = {
        async send(receiveId, content, msgType) {
            recorded.sent.push({ receiveId, content, msgType });
            return { message_id: 'om_new' };
        },
        async reply(messageId, content, msgType, inThread) {
            recorded.replied.push({ messageId, content, msgType, inThread });
            return { message_id: 'om_reply' };
        },
        async deleteMessage(messageId) {
            recorded.deleted.push(messageId);
        },
        async uploadImage(stream) {
            recorded.uploaded.push(stream);
            return { image_key: 'img_v3_uploaded' };
        },
        async getMessageInfo(messageId) {
            recorded.fetchedMessages.push(messageId);
            return {
                items: [
                    {
                        message_id: messageId,
                        chat_id: 'oc_1',
                        msg_type: 'text',
                        create_time: '1700000000000',
                        sender: { id: 'cli_chiwei', id_type: 'app_id', sender_type: 'app' },
                        body: { content: '{"text":"hi"}' },
                    },
                ],
            };
        },
        async getMessageList(params) {
            recorded.listed.push(params);
            return {
                has_more: true,
                page_token: 'next',
                items: [
                    {
                        message_id: 'om_hist',
                        chat_id: 'oc_1',
                        msg_type: 'text',
                        create_time: '1700000000000',
                        deleted: false,
                        sender: { id: 'on_someone', id_type: 'union_id' },
                        body: { content: '{"text":"过去说过的话"}' },
                        mentions: [{ key: '@_user_1', id: 'on_bot', name: '赤尾' }],
                    },
                ],
            };
        },
        async getUserInfo(userId, idType) {
            recorded.fetchedUsers.push({ userId, idType });
            return {
                user: {
                    union_id: userId,
                    open_id: 'ou_1',
                    name: '某人',
                    avatar: { avatar_origin: 'https://avatar' },
                },
            };
        },
        async downloadResource(messageId, fileKey, type) {
            recorded.downloaded.push({ messageId, fileKey, type });
            return { getReadableStream: () => Readable.from('bytes') };
        },
        async addChatMember(chatId, memberId, idType) {
            recorded.invited.push({ chatId, memberId, idType });
        },
        async request(url: string, data: unknown, method: string) {
            recorded.requested.push({ url, data, method });
            return { ok: true } as never;
        },
        ...over,
    };
    return { client, ...recorded };
}

function apiWith(client: LarkApiClient) {
    const pool: LarkClientPool = { current: () => client };
    return createSdkLarkApi(pool);
}

describe('createSdkLarkApi 发消息', () => {
    it('富文本按飞书的 post 形状发：zh_cn 包一层、msg_type=post', async () => {
        const fake = fakeClient();
        const sent = await apiWith(fake.client).sendPost('oc_chat', post);

        expect(fake.sent).toEqual([
            { receiveId: 'oc_chat', content: { zh_cn: post }, msgType: 'post' },
        ]);
        expect(sent.messageId).toBe('om_new');
    });

    it('平台没给 message_id 时如实留空，不兜成空串', async () => {
        const fake = fakeClient({ async send() {return {};} });
        expect(await apiWith(fake.client).sendPost('oc_chat', post)).toEqual({
            messageId: undefined,
        });
    });

    it('飞书那边抛出来的错误原样往上走，不吞', async () => {
        const fake = fakeClient({
            async send() {
                throw new Error('机器人不在群聊中');
            },
        });
        expect(apiWith(fake.client).sendPost('oc_chat', post)).rejects.toThrow('机器人不在群聊中');
    });
});

describe('createSdkLarkApi 回复消息', () => {
    it('回复挂在被回复那条消息上，inThread 透传', async () => {
        const fake = fakeClient();
        const sent = await apiWith(fake.client).replyPost('om_trigger', post, true);

        expect(fake.replied).toEqual([
            { messageId: 'om_trigger', content: { zh_cn: post }, msgType: 'post', inThread: true },
        ]);
        expect(sent.messageId).toBe('om_reply');
    });

    it('inThread=false 也照实传下去', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).replyPost('om_trigger', post, false);
        expect(fake.replied[0].inThread).toBe(false);
    });
});

describe('createSdkLarkApi 撤回', () => {
    it('把裸的 message id 交给飞书', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).recall('om_to_delete');
        expect(fake.deleted).toEqual(['om_to_delete']);
    });

    it('撤不掉时抛（已撤过 / 超时限都走这条）', async () => {
        const fake = fakeClient({
            async deleteMessage() {
                throw new Error('消息已被撤回或删除');
            },
        });
        expect(apiWith(fake.client).recall('om_1')).rejects.toThrow('消息已被撤回或删除');
    });
});

describe('createSdkLarkApi 上传图片', () => {
    it('把字节流交给飞书，返回 image_key', async () => {
        const fake = fakeClient();
        const key = await apiWith(fake.client).uploadImage(Buffer.from('bytes'));

        expect(key).toBe('img_v3_uploaded');
        expect(fake.uploaded).toHaveLength(1);
        expect(fake.uploaded[0]).toBeInstanceOf(Readable);
    });

    it('平台没给 key 时返回 null，让上层降级', async () => {
        const fake = fakeClient({ async uploadImage() {return {};} });
        expect(await apiWith(fake.client).uploadImage(Buffer.from('x'))).toBeNull();
    });

    it('上传抛异常时照抛，由上层统一降级', async () => {
        const fake = fakeClient({
            async uploadImage() {
                throw new Error('upload exploded');
            },
        });
        expect(apiWith(fake.client).uploadImage(Buffer.from('x'))).rejects.toThrow(
            'upload exploded',
        );
    });
});

// ---------------------------------------------------------------------------
// 指令 / 卡片回调 / 定时任务要用的那些调用（Task D0 扩的口）
// ---------------------------------------------------------------------------

describe('往会话里新发一条', () => {
    it('纯文本：msg_type=text，正文包成 { text }', async () => {
        const fake = fakeClient();
        const sent = await apiWith(fake.client).sendText('oc_chat', '日报来了');

        expect(fake.sent).toEqual([
            { receiveId: 'oc_chat', content: { text: '日报来了' }, msgType: 'text' },
        ]);
        expect(sent.messageId).toBe('om_new');
    });

    it('卡片：msg_type=interactive，卡片报文原样交出去（端口不动它的形状）', async () => {
        const fake = fakeClient();
        const card = { header: { title: '今日发图' } };
        await apiWith(fake.client).sendCard('oc_chat', card);

        expect(fake.sent).toEqual([
            { receiveId: 'oc_chat', content: card, msgType: 'interactive' },
        ]);
    });

    it('表情：msg_type=sticker，key 包成 { file_key }', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).sendSticker('oc_chat', 'sticker_key');

        expect(fake.sent).toEqual([
            { receiveId: 'oc_chat', content: { file_key: 'sticker_key' }, msgType: 'sticker' },
        ]);
    });
});

describe('挂在一条已有消息上回复', () => {
    it('纯文本回复：msg_type=text，inThread 透传', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).replyText('om_trigger', '呜呜出错了', true);

        expect(fake.replied).toEqual([
            {
                messageId: 'om_trigger',
                content: { text: '呜呜出错了' },
                msgType: 'text',
                inThread: true,
            },
        ]);
    });

    it('卡片回复：msg_type=interactive，inThread 透传', async () => {
        const fake = fakeClient();
        const card = { elements: [] };
        await apiWith(fake.client).replyCard('om_trigger', card, false);

        expect(fake.replied).toEqual([
            { messageId: 'om_trigger', content: card, msgType: 'interactive', inThread: false },
        ]);
    });

    it('图片回复：msg_type=image，key 包成 { image_key }，不进话题', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).replyImage('om_trigger', 'img_v3_1');

        expect(fake.replied).toEqual([
            {
                messageId: 'om_trigger',
                content: { image_key: 'img_v3_1' },
                msgType: 'image',
                inThread: undefined,
            },
        ]);
    });

    // 模板卡片是 interactive 里的一个子形状，不是另一种 msg_type。包错了飞书会
    // 直接拒收，而调用方看到的只是"帮助没发出去"。
    it('模板卡片：interactive 里包 { type: template, data: { template_id, template_variable } }', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).replyTemplate('om_trigger', 'ctp_AAY', { name: '赤尾' });

        expect(fake.replied).toEqual([
            {
                messageId: 'om_trigger',
                content: {
                    type: 'template',
                    data: { template_id: 'ctp_AAY', template_variable: { name: '赤尾' } },
                },
                msgType: 'interactive',
                inThread: undefined,
            },
        ]);
    });

    it('模板没有变量时如实传 undefined，不兜成空对象', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).replyTemplate('om_trigger', 'ctp_AAY');

        expect(
            (fake.replied[0]!.content as { data: { template_variable?: unknown } }).data
                .template_variable,
        ).toBeUndefined();
    });
});

describe('查', () => {
    it('查一条消息：只取第一条，字段翻成驼峰', async () => {
        const fake = fakeClient();
        const found = await apiWith(fake.client).getMessage('om_1');

        expect(fake.fetchedMessages).toEqual(['om_1']);
        expect(found).toEqual({
            messageId: 'om_1',
            chatId: 'oc_1',
            messageType: 'text',
            createTime: '1700000000000',
            senderId: 'cli_chiwei',
            senderIdType: 'app_id',
            senderType: 'app',
            content: '{"text":"hi"}',
            rootId: undefined,
            parentId: undefined,
            threadId: undefined,
            deleted: undefined,
            mentions: [],
        });
    });

    // 「撤回」指令拿 senderId 跟自己的 app_id 比。这一位要是丢了，赤尾会去撤别人的
    // 消息（撤不动，但会对着用户报一句莫名其妙的错）。
    it('查回来的发送者 id 对 bot 而言是 app_id，端口如实带上 idType', async () => {
        const fake = fakeClient();
        const found = await apiWith(fake.client).getMessage('om_1');
        expect(found!.senderIdType).toBe('app_id');
    });

    it('平台没给任何一条时返回 null，不返回半个对象', async () => {
        const fake = fakeClient({ async getMessageInfo() {return { items: [] };} });
        expect(await apiWith(fake.client).getMessage('om_gone')).toBeNull();
    });

    // 端口只认两种答案：查不到返回 null、出错抛。平台回了一条却没带 message_id 时，
    // 兜成 undefined 会让 `messageId: string` 撒谎 —— 拿它去比 app_id / 去撤回 / 去
    // 拼卡片的调用方全部在运行期读到 undefined，而类型上完全合法。
    it('平台回了一条却没带 message_id：抛，不返回 messageId 是 undefined 的对象', async () => {
        const fake = fakeClient({
            async getMessageInfo() {
                return { items: [{ chat_id: 'oc_1', msg_type: 'text' }] };
            },
        });
        await expect(apiWith(fake.client).getMessage('om_1')).rejects.toThrow(/message_id/);
    });

    it('查群历史里混进一条没有 message_id 的：同样抛', async () => {
        const fake = fakeClient({
            async getMessageList() {
                return { items: [{ chat_id: 'oc_1', msg_type: 'text' }] };
            },
        });
        await expect(apiWith(fake.client).listMessages({ chatId: 'oc_1' })).rejects.toThrow(
            /message_id/,
        );
    });

    it('查群历史：一页一次，起止时间按秒传下去，翻页 token 原样带回', async () => {
        const fake = fakeClient();
        const page = await apiWith(fake.client).listMessages({
            chatId: 'oc_1',
            startTime: 1700000000,
            endTime: 1700003600,
            pageToken: 'cursor',
        });

        expect(fake.listed).toEqual([
            {
                chatId: 'oc_1',
                startTime: 1700000000,
                endTime: 1700003600,
                pageToken: 'cursor',
            },
        ]);
        expect(page.hasMore).toBe(true);
        expect(page.pageToken).toBe('next');
        expect(page.items).toEqual([
            {
                messageId: 'om_hist',
                chatId: 'oc_1',
                messageType: 'text',
                createTime: '1700000000000',
                senderId: 'on_someone',
                senderIdType: 'union_id',
                senderType: undefined,
                content: '{"text":"过去说过的话"}',
                rootId: undefined,
                parentId: undefined,
                threadId: undefined,
                deleted: false,
                mentions: [{ key: '@_user_1', id: 'on_bot', name: '赤尾' }],
            },
        ]);
    });

    it('查群历史返回空页时 items 是空数组、hasMore 是 false，不是 undefined', async () => {
        const fake = fakeClient({ async getMessageList() {return {};} });
        expect(await apiWith(fake.client).listMessages({ chatId: 'oc_1' })).toEqual({
            items: [],
            hasMore: false,
            pageToken: undefined,
        });
    });

    it('查用户：按 union_id 查，字段翻成驼峰', async () => {
        const fake = fakeClient();
        const user = await apiWith(fake.client).getUser('on_someone');

        expect(fake.fetchedUsers).toEqual([{ userId: 'on_someone', idType: 'union_id' }]);
        expect(user).toEqual({
            unionId: 'on_someone',
            openId: 'ou_1',
            name: '某人',
            avatarOrigin: 'https://avatar',
        });
    });

    it('平台回了 code=0 但没带 user 时返回 null', async () => {
        const fake = fakeClient({ async getUserInfo() {return {};} });
        expect(await apiWith(fake.client).getUser('on_nobody')).toBeNull();
    });
});

describe('拉人回群', () => {
    it('按 open_id 拉，一次一个', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).addChatMember('oc_1', 'ou_left');
        expect(fake.invited).toEqual([
            { chatId: 'oc_1', memberId: 'ou_left', idType: 'open_id' },
        ]);
    });
});

describe('取附件字节', () => {
    it('把 message_id + file_key + 类型交给飞书，取出可读流', async () => {
        const fake = fakeClient();
        const stream = await apiWith(fake.client).downloadResource('om_1', 'img_key', 'image');

        expect(fake.downloaded).toEqual([
            { messageId: 'om_1', fileKey: 'img_key', type: 'image' },
        ]);
        expect(stream).toBeInstanceOf(Readable);
    });

    it('文件和图片走同一个口子，类型如实传', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).downloadResource('om_1', 'file_key', 'file');
        expect(fake.downloaded[0]!.type).toBe('file');
    });
});

describe('裸请求', () => {
    // 卡片延时更新和仅自己可见的卡片在 SDK 里没有对应方法，只能打裸端点。参数顺序
    // 接错的症状是 SDK 把 method 当 URL —— 请求发不出去，但错误信息跟卡片毫无关系。
    it('方法、路径、报文按飞书 SDK 的位置参数交出去', async () => {
        const fake = fakeClient();
        const body = { token: 'tk', card: { open_ids: ['ou_1'], elements: [] } };
        await apiWith(fake.client).request('POST', '/open-apis/interactive/v1/card/update', body);

        expect(fake.requested).toEqual([
            { url: '/open-apis/interactive/v1/card/update', data: body, method: 'POST' },
        ]);
    });

    it('返回值原样交给调用方（端口不解释裸端点的响应形状）', async () => {
        const fake = fakeClient();
        const result = await apiWith(fake.client).request<{ ok: boolean }>(
            'POST',
            '/open-apis/ephemeral/v1/send',
            {},
        );
        expect(result).toEqual({ ok: true });
    });

    it('裸请求失败照抛，跟别的方法一个口径', async () => {
        const fake = fakeClient({
            async request() {
                throw new Error('card token expired');
            },
        });
        expect(
            apiWith(fake.client).request('POST', '/open-apis/interactive/v1/card/update', {}),
        ).rejects.toThrow('card token expired');
    });
});

describe('接在真的 LarkClient 上', () => {
    // 上面那些用手写替身验的是本文件自己的口径。这一组换成真的 LarkClient（只把它
    // 私有持有的原生 SDK client 换掉），验的是**接起来之后**的行为 —— 飞书返回非 0
    // code 时错误确实会一路传到出站调用方，而不是被哪一层吃掉变成"发成功了"。
    function realClientWith(native: unknown): LarkClient {
        const client = new LarkClient({ appId: 'cli_test', appSecret: 'secret' });
        (client as unknown as { client: unknown }).client = native;
        return client;
    }

    it('飞书返回非 0 code 时，sendPost 抛', async () => {
        const api = apiWith(
            realClientWith({
                im: {
                    message: {
                        create: async () => ({ code: 230001, msg: 'bot is not in the chat' }),
                    },
                },
            }),
        );

        expect(api.sendPost('oc_1', post)).rejects.toThrow('bot is not in the chat');
    });

    it('飞书返回非 0 code 时，recall 抛', async () => {
        const api = apiWith(
            realClientWith({
                im: { message: { delete: async () => ({ code: 99991663, msg: 'already gone' }) } },
            }),
        );

        expect(api.recall('om_1')).rejects.toThrow('already gone');
    });

    it('上传走到真 LarkClient 也能取出 image_key', async () => {
        const api = apiWith(
            realClientWith({
                im: { v1: { image: { create: async () => ({ image_key: 'img_v3_real' }) } } },
            }),
        );

        expect(await api.uploadImage(Buffer.from('x'))).toBe('img_v3_real');
    });

    // 裸端点上没有类型可以兜底，接错了要到卡片按钮被点的那一刻才知道。所以这条一路
    // 验到 SDK 的 request：URL 和 method 真的是我们给的那两个。
    it('裸请求一路传到 SDK 的 request，URL 与 method 不走样', async () => {
        const seen: Array<{ url: string; method: string; data: unknown }> = [];
        const api = apiWith(
            realClientWith({
                request: async (options: { url: string; method: string; data: unknown }) => {
                    seen.push(options);
                    return { code: 0, data: { updated: true } };
                },
            }),
        );

        const result = await api.request<{ updated: boolean }>(
            'POST',
            '/open-apis/interactive/v1/card/update',
            { token: 'tk' },
        );

        expect(seen).toEqual([
            {
                url: '/open-apis/interactive/v1/card/update',
                method: 'POST',
                data: { token: 'tk' },
            },
        ]);
        expect(result).toEqual({ updated: true });
    });

    it('裸端点返回非 0 code 时抛，不当成成功', async () => {
        const api = apiWith(
            realClientWith({
                request: async () => ({ code: 190001, msg: 'invalid card token' }),
            }),
        );

        expect(api.request('POST', '/open-apis/ephemeral/v1/send', {})).rejects.toThrow(
            'invalid card token',
        );
    });

    it('查消息走到真 LarkClient 也能取出发送者', async () => {
        const api = apiWith(
            realClientWith({
                im: {
                    message: {
                        get: async () => ({
                            code: 0,
                            data: {
                                items: [
                                    {
                                        message_id: 'om_real',
                                        sender: { id: 'cli_real', id_type: 'app_id' },
                                    },
                                ],
                            },
                        }),
                    },
                },
            }),
        );

        expect(await api.getMessage('om_real')).toMatchObject({
            messageId: 'om_real',
            senderId: 'cli_real',
            senderIdType: 'app_id',
        });
    });
});

const credentials = (appId: string) => ({
    app_id: appId,
    app_secret: 's',
    encrypt_key: 'e',
    verification_token: 'v',
    robot_union_id: `on_${appId}`,
});

describe('larkClientPool', () => {
    it('每个 bot 各一个客户端，认名字取', () => {
        const pool = larkClientPool(
            [
                { botName: 'chiwei', credentials: credentials('cli_a') },
                { botName: 'ayana', credentials: credentials('cli_b') },
            ],
            () => 'ayana',
        );

        const first = pool.current();
        expect(first).toBeInstanceOf(LarkClient);
        // 同一个 bot 每次拿到同一个客户端：SDK 客户端内部缓存 tenant token，每次新建
        // 等于每条消息都去换一次 token。
        expect(pool.current()).toBe(first);
    });

    it('上下文里没有 bot 身份时抛，绝不挑一个默认的发出去', () => {
        const pool = larkClientPool([{ botName: 'chiwei', credentials: credentials('cli_a') }], () => '');
        expect(() => pool.current()).toThrow(/no lark bot in context/);
    });

    it('上下文说的 bot 不在本进程里时抛', () => {
        const pool = larkClientPool(
            [{ botName: 'chiwei', credentials: credentials('cli_a') }],
            () => 'someone_else',
        );
        expect(() => pool.current()).toThrow(/someone_else/);
    });
});
