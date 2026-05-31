// 生产用的 LarkOutboundDeps：把 outbound-capabilities 的 I/O 协作者接到真实
// 飞书 SDK / redis / DB。飞书 SDK 调用集中在这里 + outbound-capabilities，
// 不再散落在 worker。
//
// 拆成独立文件（不放进 outbound-capabilities.ts）是为了让纯渲染逻辑那个文件
// 在单测里零飞书/redis/DB 依赖；本文件只在运行时接线点被 import。

import { sendPost, replyPost } from '@lark/basic/message';
import { uploadImage, deleteMessage } from '@lark-client';
import { hgetall } from '@cache/redis-client';
import { resolveMentionsForGroup } from '@core/services/message/resolve-mentions';
import { Readable } from 'node:stream';
import type { PostContent } from 'types/content-types';
import type { LarkOutboundDeps } from './outbound-capabilities';

export const defaultLarkOutboundDeps: LarkOutboundDeps = {
    async send(chatId: string, content: PostContent) {
        const messageId = await sendPost(chatId, content);
        return { message_id: messageId };
    },
    async reply(messageId: string, content: PostContent, replyInThread: boolean) {
        const newId = await replyPost(messageId, content, replyInThread);
        return { message_id: newId };
    },
    async deleteMessage(messageId: string) {
        return deleteMessage(messageId);
    },
    async uploadImage(image: Buffer) {
        // @lark-client uploadImage 返回 ... | undefined（其内部 raw 返回可能是
        // null）；端口签名是 ... | undefined，这里把 null 归一成 undefined。
        return (await uploadImage(Readable.from(image))) ?? undefined;
    },
    async getImageRegistry(key: string) {
        return hgetall(key);
    },
    async resolveMentionsForGroup(content: string, chatId: string) {
        return resolveMentionsForGroup(content, chatId);
    },
    async fetchImage(url: string) {
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`failed to download image: HTTP ${response.status}`);
        }
        return Buffer.from(await response.arrayBuffer());
    },
};
