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
import type { LarkOutboundApi, LarkSentMessage } from './lark-api';
import type { PostContent } from './post-content';

/**
 * 出站用到的那四个 LarkClient 方法。
 *
 * 写成 Pick 而不是抄一遍签名：真的 LarkClient 天然满足它，测试的手写替身也满足它，
 * 而 lark-utils 那边改了签名这里会编译期报错。
 */
export type LarkApiClient = Pick<
    LarkClient,
    'send' | 'reply' | 'deleteMessage' | 'uploadImage'
>;

/** 这次调用该用哪个 bot 的飞书客户端。 */
export interface LarkClientPool {
    current(): LarkApiClient;
}

export interface LarkOutboundBot {
    botName: string;
    credentials: LarkCredentials;
}

/** 飞书富文本消息的类型标记。 */
const POST_MSG_TYPE = 'post';

/** 富文本的语言键。只发中文 —— 赤尾不说别的语言，多列一个键飞书也只挑一个用。 */
const wrapPost = (content: PostContent) => ({ zh_cn: content });

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
    return {
        async sendPost(chatId, content): Promise<LarkSentMessage> {
            const resp = await pool.current().send(chatId, wrapPost(content), POST_MSG_TYPE);
            return { messageId: resp?.message_id };
        },

        async replyPost(messageId, content, inThread): Promise<LarkSentMessage> {
            const resp = await pool
                .current()
                .reply(messageId, wrapPost(content), POST_MSG_TYPE, inThread);
            return { messageId: resp?.message_id };
        },

        async recall(messageId): Promise<void> {
            await pool.current().deleteMessage(messageId);
        },

        async uploadImage(image): Promise<string | null> {
            // SDK 要一个流。Buffer 直接给也能跑，但换成流是线上已经在跑的形态，不在
            // 拆分这一批里顺手改。
            const resp = await pool.current().uploadImage(Readable.from(image));
            return resp?.image_key ?? null;
        },
    };
}
