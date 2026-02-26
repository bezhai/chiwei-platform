import { ValidLarkCard } from 'feishu-card';
import { reply, getMessageList, send } from '@lark-client';
import { PostContent } from 'types/content-types';
import { RateLimiter } from '@inner/shared';
import { Message } from 'core/models/message';

export async function sendMsg(chat_id: string, message: string) {
    return send(chat_id, { text: message }, 'text');
}

export async function sendSticker(chat_id: string, sticker_id: string) {
    await send(chat_id, { file_key: sticker_id }, 'sticker');
}

export async function replyMessage(messageId: string, message: string, replyInThread?: boolean) {
    await reply(messageId, { text: message }, 'text', replyInThread);
}

export async function sendPost(chat_id: string, content: PostContent) {
    await send(chat_id, { zh_cn: content }, 'post');
}

export async function replyPost(messageId: string, content: PostContent, replyInThread?: boolean) {
    await reply(messageId, { zh_cn: content }, 'post', replyInThread);
}

export async function sendCard(chat_id: string, card: ValidLarkCard) {
    await send(chat_id, card, 'interactive');
}

export async function replyCard(messageId: string, card: ValidLarkCard, replyInThread?: boolean) {
    await reply(messageId, card, 'interactive', replyInThread);
}

export async function replyImage(messageId: string, image_key: string) {
    await reply(messageId, { image_key }, 'image');
}

export async function replyTemplate(
    messageId: string,
    template_id: string,
    template_variable: any,
) {
    await reply(
        messageId,
        { type: 'template', data: { template_id, template_variable } },
        'interactive',
    );
}

const minuteLimiter = new RateLimiter(800, 60 * 1000); // 每分钟限制800次
const secondLimiter = new RateLimiter(40, 1000); // 每秒限制40次

export async function searchGroupMessage(chat_id: string, start_time: number, end_time: number) {
    let pageToken: string | undefined = undefined;

    const messageList: Message[] = [];

    while (true) {
        await minuteLimiter.waitForAllowance(60 * 1000);
        await secondLimiter.waitForAllowance(10 * 1000);

        const res = await getMessageList(chat_id, start_time, end_time, pageToken);
        if (res?.items) {
            messageList.push(
                ...res.items
                    .filter((item) => !item.deleted && item.msg_type !== 'merge_forward')
                    .map((item) => Message.fromHistoryMessage(item)),
            );
            pageToken = res?.page_token;
        }

        if (!res?.has_more) {
            break;
        }
    }

    return messageList;
}
