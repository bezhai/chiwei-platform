// LarkOutboundApi 的真身。**本服务里唯一持有飞书 SDK 客户端的地方。**
//
// SDK 的薄封装（把 content JSON 序列化、按 code 判成败、把已知错误码翻成人话、上传
// 接口的返回值怎么取）不在这里写第二遍 —— 那份封装是 @inner/lark-utils 的
// LarkClient，本文件只做它没法做的两件事：
//
//   1. **这次调用该用哪个 bot 的客户端。** 客户端按 bot 分池，因为每个 bot 是一个独立
//      的飞书应用、各有各的 app_id/app_secret 和 tenant token。选哪个由请求上下文决定，
//      而 lark-utils 那个 manager 是靠一个进程级可变字段记"当前 bot"的 —— 出站是并发
//      消费，两条消息交错跑就会互相改掉对方的当前 bot，从错的 bot 发出去。所以选 bot
//      走 AsyncLocalStorage，不用它。
//   2. **富文本消息的形状。** 飞书的 post 要包一层语言：`{ zh_cn: <PostContent> }`。
//
// 端口约定的失败行为（一律抛）在这里靠"什么都不 catch"实现。唯一处理的是 uploadImage
// 没拿到 key 的情况，归一成 null。

import { createLarkClient, type LarkClient } from '@inner/lark-utils';
import { context } from '@inner/shared/middleware';
import { Readable } from 'node:stream';

import type { LarkCredentials } from '../credentials';
import type {
    LarkCard,
    LarkMessageInfo,
    LarkMessagePage,
    LarkOutboundApi,
    LarkSentMessage,
    LarkUserInfo,
} from './lark-api';
import type { PostContent } from './post-content';

/**
 * 端口用到的那些 LarkClient 方法。
 *
 * 写成 Pick 而不是抄一遍签名：真的 LarkClient 天然满足它，测试的手写替身也满足它，
 * 而 lark-utils 那边改了签名这里会编译期报错。
 */
export type LarkApiClient = Pick<
    LarkClient,
    | 'send'
    | 'reply'
    | 'deleteMessage'
    | 'uploadImage'
    | 'getMessageInfo'
    | 'getMessageList'
    | 'getUserInfo'
    | 'downloadResource'
    | 'addChatMember'
    | 'request'
>;

/** 这次调用该用哪个 bot 的飞书客户端。 */
export interface LarkClientPool {
    current(): LarkApiClient;
}

export interface LarkOutboundBot {
    botName: string;
    credentials: LarkCredentials;
}

/** 飞书的消息类型标记。 */
const POST_MSG_TYPE = 'post';
const TEXT_MSG_TYPE = 'text';
const CARD_MSG_TYPE = 'interactive';
const STICKER_MSG_TYPE = 'sticker';
const IMAGE_MSG_TYPE = 'image';

/** 富文本的语言键。只发中文 —— 赤尾不说别的语言，多列一个键飞书也只挑一个用。 */
const wrapPost = (content: PostContent) => ({ zh_cn: content });

/**
 * 飞书查回来的那些形状。SDK 那边这些接口的返回类型是 any，所以在这里写一遍 ——
 * 唯一一处知道"飞书的字段叫什么"的地方，端口以上一律是驼峰。
 */
interface RawLarkMessage {
    message_id?: string;
    chat_id?: string;
    msg_type?: string;
    create_time?: string;
    root_id?: string;
    parent_id?: string;
    thread_id?: string;
    deleted?: boolean;
    sender?: { id?: string; id_type?: string; sender_type?: string };
    body?: { content?: string };
    mentions?: Array<{ key?: string; id?: string; name?: string }>;
}

/**
 * 一条查回来的消息翻成端口的口径。飞书没给的字段如实留空，不兜默认值。
 *
 * **唯一一个不能留空的是 message_id**，所以它在这里校验而不是断言。端口对查询只认
 * 两种答案：查不到返回 null、出错抛（见 lark-api.ts 的文件头）。平台回了一条却没带
 * id 时，`raw.message_id!` 会造出第三种 —— 一个 `messageId` 在类型上是 string、运行
 * 期是 undefined 的对象。拿它去跟 app_id 比「这条是不是我发的」、去撤回、去拼卡片的
 * 调用方全都读到 undefined，而每一处都类型合法、没有任何报错。
 */
function messageInfoOf(raw: RawLarkMessage): LarkMessageInfo {
    if (!raw.message_id) {
        throw new Error(
            'lark returned a message without a message_id; ' +
                'the port cannot describe it (a message id is not optional here)',
        );
    }
    return {
        messageId: raw.message_id,
        chatId: raw.chat_id,
        senderId: raw.sender?.id,
        senderIdType: raw.sender?.id_type,
        senderType: raw.sender?.sender_type,
        messageType: raw.msg_type,
        createTime: raw.create_time,
        content: raw.body?.content,
        mentions: raw.mentions ?? [],
        rootId: raw.root_id,
        parentId: raw.parent_id,
        threadId: raw.thread_id,
        deleted: raw.deleted,
    };
}

/**
 * 生产用的客户端池。
 *
 * 每个 bot 建一个客户端并**一直留着**：SDK 客户端内部缓存 tenant access token，每次
 * 新建等于每条消息都去飞书换一次 token。
 *
 * 当前 bot 默认从请求上下文取（AsyncLocalStorage，并发安全）。**取不到就抛，不挑一个
 * 默认的** —— 出站发错 bot 的后果是用户看见另一个人设开口说话，比不发严重得多。
 */
export function larkClientPool(
    bots: readonly LarkOutboundBot[],
    currentBotName: () => string = () => context.getBotName(),
): LarkClientPool {
    const clients = new Map<string, LarkClient>();
    for (const bot of bots) {
        clients.set(
            bot.botName,
            createLarkClient({
                appId: bot.credentials.app_id,
                appSecret: bot.credentials.app_secret,
                botName: bot.botName,
            }),
        );
    }

    return {
        current() {
            const botName = currentBotName();
            if (!botName) {
                throw new Error(
                    'no lark bot in context; refusing to guess which bot should speak',
                );
            }

            const client = clients.get(botName);
            if (!client) {
                throw new Error(
                    `no lark client for bot "${botName}" in this process; ` +
                        'it is not one of the bots this deployment loaded',
                );
            }
            return client;
        },
    };
}

export function createSdkLarkApi(pool: LarkClientPool): LarkOutboundApi {
    const sent = (resp: { message_id?: string } | undefined): LarkSentMessage => ({
        messageId: resp?.message_id,
    });

    return {
        // ---- 发 ----

        async sendPost(chatId, content): Promise<LarkSentMessage> {
            return sent(await pool.current().send(chatId, wrapPost(content), POST_MSG_TYPE));
        },

        async sendText(chatId, text): Promise<LarkSentMessage> {
            return sent(await pool.current().send(chatId, { text }, TEXT_MSG_TYPE));
        },

        async sendCard(chatId, card: LarkCard): Promise<LarkSentMessage> {
            return sent(await pool.current().send(chatId, card, CARD_MSG_TYPE));
        },

        async sendSticker(chatId, fileKey): Promise<LarkSentMessage> {
            return sent(
                await pool.current().send(chatId, { file_key: fileKey }, STICKER_MSG_TYPE),
            );
        },

        // ---- 回 ----

        async replyPost(messageId, content, inThread): Promise<LarkSentMessage> {
            return sent(
                await pool.current().reply(messageId, wrapPost(content), POST_MSG_TYPE, inThread),
            );
        },

        async replyText(messageId, text, inThread): Promise<LarkSentMessage> {
            return sent(
                await pool.current().reply(messageId, { text }, TEXT_MSG_TYPE, inThread),
            );
        },

        async replyCard(messageId, card: LarkCard, inThread): Promise<LarkSentMessage> {
            return sent(await pool.current().reply(messageId, card, CARD_MSG_TYPE, inThread));
        },

        async replyImage(messageId, imageKey): Promise<LarkSentMessage> {
            return sent(
                await pool.current().reply(messageId, { image_key: imageKey }, IMAGE_MSG_TYPE),
            );
        },

        async replyTemplate(messageId, templateId, variables): Promise<LarkSentMessage> {
            // 模板卡片是 interactive 里的一个子形状，包法定在这里 —— 每个调用点各包
            // 一遍的话，包错的那一个要到用户敲「帮助」时才暴露。
            return sent(
                await pool.current().reply(
                    messageId,
                    {
                        type: 'template',
                        data: { template_id: templateId, template_variable: variables },
                    },
                    CARD_MSG_TYPE,
                ),
            );
        },

        // ---- 撤回 ----

        async recall(messageId): Promise<void> {
            await pool.current().deleteMessage(messageId);
        },

        // ---- 查 ----

        async getMessage(messageId): Promise<LarkMessageInfo | null> {
            const resp = (await pool.current().getMessageInfo(messageId)) as
                | { items?: RawLarkMessage[] }
                | undefined;
            // 按主键查却返回列表，是这个接口为合并转发留的形状，不是语义。没有第一条
            // 就是"这条消息不在了"，端口把它说成 null 而不是让调用方去解列表。
            const first = resp?.items?.[0];
            return first ? messageInfoOf(first) : null;
        },

        async listMessages(query): Promise<LarkMessagePage> {
            const resp = (await pool.current().getMessageList({
                chatId: query.chatId,
                startTime: query.startTime,
                endTime: query.endTime,
                pageToken: query.pageToken,
            })) as
                | { items?: RawLarkMessage[]; has_more?: boolean; page_token?: string }
                | undefined;
            return {
                items: (resp?.items ?? []).map(messageInfoOf),
                // 平台没说就是没有下一页。undefined 交出去会让 `while (page.hasMore)`
                // 这种写法退化成"永远只取一页"，而那看起来完全正常。
                hasMore: resp?.has_more ?? false,
                pageToken: resp?.page_token,
            };
        },

        async getUser(unionId): Promise<LarkUserInfo | null> {
            const resp = (await pool.current().getUserInfo(unionId, 'union_id')) as
                | {
                      user?: {
                          union_id?: string;
                          open_id?: string;
                          name?: string;
                          avatar?: { avatar_origin?: string };
                      };
                  }
                | undefined;
            const user = resp?.user;
            if (!user) return null;
            return {
                unionId: user.union_id,
                openId: user.open_id,
                name: user.name,
                avatarOrigin: user.avatar?.avatar_origin,
            };
        },

        // ---- 改群 ----

        async addChatMember(chatId, openId): Promise<void> {
            await pool.current().addChatMember(chatId, openId, 'open_id');
        },

        // ---- 取字节 ----

        async downloadResource(messageId, fileKey, type): Promise<Readable> {
            // SDK 交回来的不是流本身，是一个能开流的句柄（它还能直接落盘、取文件名）。
            // 端口只要字节，所以在这里就把流取出来。
            const resp = (await pool.current().downloadResource(messageId, fileKey, type)) as {
                getReadableStream(): Readable;
            };
            return resp.getReadableStream();
        },

        async uploadImage(image): Promise<string | null> {
            // SDK 要一个流。Buffer 直接给也能跑，但换成流是线上已经在跑的形态，不在
            // 拆分这一批里顺手改。
            const resp = await pool.current().uploadImage(Readable.from(image));
            return resp?.image_key ?? null;
        },

        // ---- 逃生口 ----

        request<T>(method: string, path: string, body: unknown): Promise<T> {
            // 真身的位置参数是 (url, data, method) —— 跟这里的读法反着来。翻译只在这
            // 一处做，接错了 SDK 会把 method 当 URL 打出去，报错跟卡片毫无关系。
            return pool.current().request<T>(path, body, method);
        },
    };
}
