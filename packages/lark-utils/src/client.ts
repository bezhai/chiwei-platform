import * as lark from '@larksuiteoapi/node-sdk';
import { Readable } from 'node:stream';
import { ReadStream } from 'node:fs';
import {
    LarkClientConfig,
    createDefaultLarkConfig,
    LarkResponse,
    MessageType,
    ReceiveIdType,
    SendMessageParams,
    ReplyMessageParams,
    GetMessageListParams,
    ChatMember,
    ChatInfo,
    UserInfo,
    MessageInfo,
    ERROR_CODE_MAP,
    withLarkErrorCode,
} from './types';

/**
 * Lark 客户端封装类
 */
export class LarkClient {
    private client: lark.Client;
    private config: LarkClientConfig;

    constructor(config?: Partial<LarkClientConfig>) {
        this.config = { ...createDefaultLarkConfig(), ...config };
        this.client = new lark.Client({
            appId: this.config.appId,
            appSecret: this.config.appSecret,
        });
    }

    /**
     * 获取原生 Lark SDK 客户端
     */
    getNativeClient(): lark.Client {
        return this.client;
    }

    /**
     * 处理 API 响应
     *
     * 两条抛错路径都把飞书那个数字 code 挂在抛出的 Error 上（见 types.ts 的
     * withLarkErrorCode）。**只是多挂一个 non-enumerable 属性**：类型、文案、可枚举
     * 字段一个都没动，只读 message 的调用方看不出区别。挂它是因为飞书用数字码区分
     * 失败的种类，而"这一种该怎么办"只有业务层知道 —— 撤回那一侧要把「消息已被撤回
     * 或删除」跟「超出撤回时限」分开处置，光有一句 msg 就只能拿文案去匹配。
     */
    private async handleResponse<T>(promise: Promise<LarkResponse<T>>): Promise<T> {
        try {
            const res = await promise;
            if (res.code !== 0) {
                throw withLarkErrorCode(new Error(res.msg), res.code);
            }
            return res.data!;
        } catch (e: any) {
            console.error(JSON.stringify(e.response?.data || e, null, 4));
            if (e.response?.data?.code) {
                throw withLarkErrorCode(
                    new Error(ERROR_CODE_MAP[e.response?.data?.code] || e.response?.data?.msg),
                    e.response?.data?.code,
                );
            }
            throw e;
        }
    }

    // ==================== 消息相关 ====================

    /**
     * 发送消息
     */
    async send(
        receiveId: string,
        content: any,
        msgType: MessageType,
        receiveIdType: ReceiveIdType = 'chat_id'
    ): Promise<any> {
        return this.handleResponse(
            this.client.im.message.create({
                params: { receive_id_type: receiveIdType },
                data: {
                    receive_id: receiveId,
                    content: JSON.stringify(content),
                    msg_type: msgType,
                },
            })
        );
    }

    /**
     * 发送文本消息
     */
    async sendText(chatId: string, text: string): Promise<any> {
        return this.send(chatId, { text }, 'text');
    }

    /**
     * 发送卡片消息
     */
    async sendCard(chatId: string, card: any): Promise<any> {
        return this.send(chatId, card, 'interactive');
    }

    /**
     * 发送表情包
     */
    async sendSticker(chatId: string, stickerId: string): Promise<any> {
        return this.send(chatId, { file_key: stickerId }, 'sticker');
    }

    /**
     * 发送图片
     */
    async sendImage(chatId: string, imageKey: string): Promise<any> {
        return this.send(chatId, { image_key: imageKey }, 'image');
    }

    /**
     * 回复消息
     */
    async reply(
        messageId: string,
        content: any,
        msgType: MessageType,
        replyInThread?: boolean
    ): Promise<any> {
        return this.handleResponse(
            this.client.im.message.reply({
                path: { message_id: messageId },
                data: {
                    content: JSON.stringify(content),
                    msg_type: msgType,
                    reply_in_thread: replyInThread,
                },
            })
        );
    }

    /**
     * 回复文本消息
     */
    async replyText(messageId: string, text: string, replyInThread?: boolean): Promise<any> {
        return this.reply(messageId, { text }, 'text', replyInThread);
    }

    /**
     * 回复卡片消息
     */
    async replyCard(messageId: string, card: any, replyInThread?: boolean): Promise<any> {
        return this.reply(messageId, card, 'interactive', replyInThread);
    }

    /**
     * 回复图片
     */
    async replyImage(messageId: string, imageKey: string): Promise<any> {
        return this.reply(messageId, { image_key: imageKey }, 'image');
    }

    /**
     * 获取消息信息
     */
    async getMessageInfo(messageId: string): Promise<any> {
        return this.handleResponse(
            this.client.im.message.get({
                path: { message_id: messageId },
                params: { user_id_type: 'union_id' },
            })
        );
    }

    /**
     * 删除消息
     */
    async deleteMessage(messageId: string): Promise<void> {
        await this.handleResponse(
            this.client.im.message.delete({
                path: { message_id: messageId },
            })
        );
    }

    /**
     * 获取消息列表
     */
    async getMessageList(params: GetMessageListParams): Promise<any> {
        return this.handleResponse(
            this.client.im.message.list({
                params: {
                    page_size: params.pageSize || 50,
                    page_token: params.pageToken,
                    start_time: params.startTime?.toString(),
                    end_time: params.endTime?.toString(),
                    container_id_type: 'chat',
                    container_id: params.chatId,
                    sort_type: 'ByCreateTimeAsc',
                },
            })
        );
    }

    // ==================== 群聊相关 ====================

    /**
     * 获取群聊列表
     */
    async getChatList(pageToken?: string): Promise<{
        items?: ChatInfo[];
        has_more?: boolean;
        page_token?: string;
    }> {
        return this.handleResponse(
            this.client.im.chat.list({
                params: { page_size: 100, page_token: pageToken, sort_type: 'ByCreateTimeAsc' },
            })
        );
    }

    /**
     * 获取群聊信息
     */
    async getChatInfo(chatId: string): Promise<ChatInfo> {
        return this.handleResponse(
            this.client.im.chat.get({
                path: { chat_id: chatId },
                params: { user_id_type: 'union_id' },
            })
        );
    }

    /**
     * 获取群成员列表
     */
    async getChatMembers(
        chatId: string,
        pageToken?: string,
        memberIdType: 'user_id' | 'union_id' | 'open_id' = 'union_id'
    ): Promise<{
        items?: ChatMember[];
        has_more?: boolean;
        page_token?: string;
        member_total?: number;
    }> {
        return this.handleResponse(
            this.client.im.chatMembers.get({
                path: { chat_id: chatId },
                params: { page_size: 50, page_token: pageToken, member_id_type: memberIdType },
            })
        );
    }

    /**
     * 添加群成员
     */
    async addChatMember(
        chatId: string,
        memberId: string,
        memberIdType: 'user_id' | 'union_id' | 'open_id' = 'open_id'
    ): Promise<void> {
        await this.handleResponse(
            this.client.im.chatMembers.create({
                path: { chat_id: chatId },
                params: { member_id_type: memberIdType },
                data: {
                    id_list: [memberId],
                },
            })
        );
    }

    // ==================== 用户相关 ====================

    /**
     * 获取用户信息
     */
    async getUserInfo(
        userId: string,
        userIdType: 'user_id' | 'union_id' | 'open_id' = 'union_id'
    ): Promise<any> {
        return this.handleResponse(
            this.client.contact.v3.user.get({
                path: { user_id: userId },
                params: { user_id_type: userIdType },
            })
        );
    }

    // ==================== 资源相关 ====================

    /**
     * 下载消息中的资源
     */
    async downloadResource(
        messageId: string,
        fileKey: string,
        type: 'image' | 'file'
    ): Promise<any> {
        return this.client.im.v1.messageResource.get({
            path: {
                message_id: messageId,
                file_key: fileKey,
            },
            params: { type },
        });
    }

    /**
     * 下载图片
     */
    async downloadImage(imageKey: string): Promise<any> {
        return this.client.im.v1.image.get({
            path: { image_key: imageKey },
        });
    }

    /**
     * 上传图片
     *
     * **上传接口的返回值不是 { code, msg, data } 信封。** SDK 对 im.v1.image.create
     * 直接给解包后的 `{ image_key } | null`（见 @larksuiteoapi/node-sdk 的类型声明），
     * 所以这里不能再取一层 .data —— 取了就永远拿到 undefined，症状是每张图都
     * "上传失败"，而且不抛错、日志里什么都没有。
     */
    async uploadImage(fileStream: Readable): Promise<{ image_key?: string }> {
        const result = await this.client.im.v1.image.create({
            data: {
                image: fileStream as ReadStream,
                image_type: 'message',
            },
        });
        return result ?? {};
    }

    // ==================== 通用请求 ====================

    /**
     * 发送自定义请求
     */
    async request<T>(url: string, data: any, method: string): Promise<T> {
        return this.handleResponse(
            this.client.request<LarkResponse<T>>({
                url,
                method,
                data,
            })
        );
    }
}

// 默认单例实例
let defaultInstance: LarkClient | null = null;

/**
 * 获取默认 Lark 客户端实例
 */
export function getLarkClient(config?: Partial<LarkClientConfig>): LarkClient {
    if (!defaultInstance) {
        defaultInstance = new LarkClient(config);
    }
    return defaultInstance;
}

/**
 * 重置默认 Lark 客户端实例
 */
export function resetLarkClient(): void {
    defaultInstance = null;
}

/**
 * 创建新的 Lark 客户端实例
 */
export function createLarkClient(config?: Partial<LarkClientConfig>): LarkClient {
    return new LarkClient(config);
}
