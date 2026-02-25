import { StreamAction } from 'types/ai';
import dayjs from 'dayjs';
import { CardBuilder, ELEMENT_IDS } from './card-builder';
import { CardApiClient } from './card-api-client';
import { CardContext, CardContextService } from './card-context-service';
import { LarkCard } from 'feishu-card';

/**
 * Action内容适配器
 * 负责将action中的链接引用转换为number_tag格式
 */
export class ActionContentAdapter {
    private refLinkMap = new Map<string, number>();
    private refCounter = 1;
    private readonly refRegex = /[(\（]ref:(https?:\/\/[^\)）]+)[\)）]/g;

    /**
     * 转换内容中的链接引用
     */
    private transformContent(content: string): string {
        return content.replace(this.refRegex, (match, url) => {
            if (!this.refLinkMap.has(url)) {
                this.refLinkMap.set(url, this.refCounter++);
            }
            const number = this.refLinkMap.get(url)!;
            return `<number_tag background_color='grey-50' font_color='grey-600' url='${url}'>${number}</number_tag>`;
        });
    }

    /**
     * 包装原始的action处理器，添加内容转换功能
     */
    public wrapActionHandler(originalHandler: (action: StreamAction) => Promise<void>) {
        return async (action: StreamAction) => {
            const transformedAction = {
                ...action,
                content: this.transformContent(action.content),
            };
            return originalHandler(transformedAction);
        };
    }
}

/**
 * CardLifecycleManager 统一管理飞书卡片的全生命周期
 * 包括创建、更新、删除等所有操作
 */
export class CardLifecycleManager {
    private cardBuilder: CardBuilder;
    private apiClient: CardApiClient;
    private contextService: CardContextService;

    private card: LarkCard;
    private cardId?: string;
    private messageId?: string;
    private sequence: number = 0;
    private hasReasoningElement: boolean = false;
    private hasResponseElement: boolean = false;
    private createTime: number; // 创建时间, 毫秒时间戳
    private cardContext: Record<string, any> = {}; // 额外数据, 会写到回调里
    private actionContentAdapter: ActionContentAdapter;

    private constructor() {
        this.cardBuilder = new CardBuilder();
        this.apiClient = new CardApiClient();
        this.contextService = new CardContextService();
        this.card = this.cardBuilder.buildInitialCard();
        this.createTime = dayjs().valueOf();
        this.actionContentAdapter = new ActionContentAdapter();
    }

    public getCreateTime(): number {
        return this.createTime;
    }

    public appendCardContext(context: Record<string, any>): void {
        this.cardContext = { ...this.cardContext, ...context };
    }

    public static init(): CardLifecycleManager {
        return new CardLifecycleManager();
    }

    private async addInitialElements(): Promise<void> {
        const elements = this.cardBuilder.buildInitialElements();
        if (this.cardId) {
            await this.apiClient.addElements(this.cardId, 'append', elements, this.getSequence());
        } else {
            this.card.addElement(...elements);
        }
    }

    private async registerReply() {
        await this.create();
        await this.addInitialElements();
    }

    public static async loadFromMessage(messageId: string): Promise<CardLifecycleManager | null> {
        const contextService = new CardContextService();
        const cardContext = await contextService.loadContext(messageId);
        if (!cardContext) {
            return null;
        }

        const instance = CardLifecycleManager.init();
        instance.cardId = cardContext.card_id;
        instance.messageId = messageId;
        instance.sequence = cardContext.sequence;

        await instance.apiClient.updateCard(instance.cardId, instance.card, instance.getSequence());
        await instance.addInitialElements();

        return instance;
    }

    public async complete(): Promise<void> {
        if (!this.cardId) {
            return;
        }
        await this.saveContext();
    }

    private getSequence(): number {
        return ++this.sequence;
    }

    private async saveContext(): Promise<void> {
        if (!this.cardId) return;

        const context: CardContext = {
            card_id: this.cardId,
            message_id: this.messageId || '',
            chat_id: this.messageId?.split('_')[0] || '',
            sequence: this.sequence,
            last_updated: new Date(),
        };
        await this.contextService.saveContext(context);
    }

    private async create(): Promise<void> {
        if (this.cardId) {
            throw new Error('Card already exists');
        }
        this.cardId = await this.apiClient.createCard(this.card);
        await this.saveContext();
    }

    public async replyToMessage(messageId: string): Promise<void> {
        if (!this.cardId) {
            throw new Error('Card not created yet');
        }
        this.messageId = await this.apiClient.replyToMessage(messageId, this.cardId);
        await this.complete();
    }

    public async sendToChat(chatId: string): Promise<void> {
        if (!this.cardId) {
            throw new Error('Card not created yet');
        }
        this.messageId = await this.apiClient.sendToChat(chatId, this.cardId);
        await this.complete();
    }

    private async createReasoningElement(): Promise<void> {
        if (!this.hasReasoningElement) {
            const collapseElement = this.cardBuilder.buildReasoningElement();
            await this.apiClient.addElements(this.cardId!, 'insert_before', [collapseElement], this.getSequence(), ELEMENT_IDS.HR);
            this.hasReasoningElement = true;
        }
    }

    private async createResponseElement(): Promise<void> {
        if (!this.hasResponseElement) {
            const mdElement = this.cardBuilder.buildResponseElement();
            await this.apiClient.addElements(this.cardId!, 'insert_before', [mdElement], this.getSequence(), ELEMENT_IDS.HR);
            this.hasResponseElement = true;
        }
    }

    public async updateThinking(content: string): Promise<void> {
        await this.createReasoningElement();
        await this.apiClient.streamUpdateText(this.cardId!, ELEMENT_IDS.REASONING, content, this.getSequence());
    }

    public async updateContent(content: string): Promise<void> {
        await this.createResponseElement();
        const result = await this.apiClient.streamUpdateText(this.cardId!, ELEMENT_IDS.RESPONSE, content, this.getSequence());
        console.info(`update msgId: ${this.getMessageId()}, content: [${content}], result: ${JSON.stringify(result)}`);
    }

    public async updateStatus(statusMessage: string): Promise<void> {
        if (this.cardId) {
            await this.apiClient.streamUpdateText(this.cardId, ELEMENT_IDS.THINKING_PLACEHOLDER, statusMessage, this.getSequence());
        }
    }

    private async removeLoadingElements(): Promise<void> {
        await this.apiClient.deleteElement(this.cardId!, ELEMENT_IDS.THINKING_PLACEHOLDER, this.getSequence());
        await this.apiClient.deleteElement(this.cardId!, ELEMENT_IDS.HR, this.getSequence());
    }

    private async addInteractionElements(): Promise<void> {
        const columnSet = this.cardBuilder.buildInteractionElements(this.messageId!, this.cardContext);
        await this.apiClient.addElements(this.cardId!, 'append', [columnSet], this.getSequence());
    }

    private async handleError(): Promise<void> {
        const errorElement = this.cardBuilder.buildErrorElement();
        await this.apiClient.addElements(this.cardId!, 'append', [errorElement], this.getSequence());
    }

    private async handleSuccess(fullText: string): Promise<void> {
        const removeThinkText = fullText.replace(/<think>[\s\S]*?<\/think>/g, '');
        await this.apiClient.updateCardSettings(this.cardId!, {
            streaming_mode: false,
            summary: {
                content: CardBuilder.truncate(removeThinkText, 20),
            },
        }, this.getSequence());
    }

    public createActionHandler(): (action: StreamAction) => Promise<void> {
        const originalHandler = async (action: StreamAction) => {
            try {
                switch (action.type) {
                    case 'think':
                        if (action.content.length > 0) {
                            await this.updateThinking(action.content);
                        }
                        break;
                    case 'text':
                        if (action.content.length > 0) {
                            await this.updateContent(action.content);
                        }
                        break;
                    case 'status':
                        if (action.content.length > 0) {
                            await this.updateStatus(action.content);
                        }
                        break;
                }
            } catch (error) {
                console.error('处理action时出错:', error);
            }
        };
        return this.actionContentAdapter.wrapActionHandler(originalHandler);
    }

    public getMessageId(): string | undefined {
        return this.messageId;
    }

    public getCardId(): string | undefined {
        return this.cardId;
    }

    public createAcceptHandler(): () => Promise<void> {
        return async () => {
            console.debug('消息已被接收');
        };
    }

    public createStartReplyHandler(): () => Promise<void> {
        return async () => {
            console.debug('开始注册卡片回复...');
            await this.registerReply();
        };
    }

    public createReplyToMessageHandler(messageId: string): () => Promise<void> {
        return async () => {
            await this.replyToMessage(messageId);
            console.debug('卡片回复注册完成');
        };
    }

    public createSuccessHandler(): (content: string) => Promise<void> {
        return async (content: string) => {
            console.debug('聊天处理成功');
            await this.handleSuccessOnly(content);
        };
    }

    public createFailedHandler(): (error: Error) => Promise<void> {
        return async (error: Error) => {
            console.error('聊天处理失败:', error);
            await this.handleErrorOnly();
        };
    }

    public createEndHandler(): () => Promise<void> {
        return async () => {
            console.debug('聊天会话结束');
            await this.complete();
        };
    }

    public createAdvancedCallbacks(messageId: string) {
        return {
            onAccept: this.createAcceptHandler(),
            onStartReply: async () => {
                await this.createStartReplyHandler()();
                await this.createReplyToMessageHandler(messageId)();
            },
            onSend: this.createActionHandler(),
            onSuccess: this.createSuccessHandler(),
            onFailed: this.createFailedHandler(),
            onEnd: this.createEndHandler(),
        };
    }

    private async handleSuccessOnly(fullText: string): Promise<void> {
        if (!this.cardId) {
            return;
        }
        await this.removeLoadingElements();
        await this.handleSuccess(fullText);
        await this.addInteractionElements();
    }

    private async handleErrorOnly(): Promise<void> {
        if (!this.cardId) {
            return; // 如果还没有创建卡片，说明连接在很早期就失败了，无需显示错误卡片
        }
        await this.removeLoadingElements();
        await this.handleError();
        await this.addInteractionElements();
    }
}