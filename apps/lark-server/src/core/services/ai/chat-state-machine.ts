import { StreamAction } from 'types/ai';
import { Step } from 'types/chat';
import { StateMachine, StateTransition } from 'utils/state-machine/state-machine';

/**
 * 聊天状态机的数据类型
 */
export interface ChatStateData {
    step: Step;
    content?: string;
    reason_content?: string;
    status_message?: string;
}

/**
 * 聊天状态机的回调接口
 */
export interface ChatStateMachineCallbacks {
    onAccept?: () => Promise<void>;
    onStartReply?: () => Promise<void>;
    onSend?: (action: StreamAction) => Promise<void>;
    onSuccess?: (content: string) => Promise<void>;
    onFailed?: (error: Error) => Promise<void>;
    onEnd?: () => Promise<void>;
}

/**
 * 创建聊天状态机
 */
export function createChatStateMachine(
    callbacks: ChatStateMachineCallbacks,
): StateMachine<Step, ChatStateData> {
    // 定义状态转换规则
    const transitions: StateTransition<Step>[] = [
        // 正常流程
        { from: Step.ACCEPT, to: Step.ACCEPT }, // 初始化
        { from: Step.ACCEPT, to: Step.START_REPLY },
        { from: [Step.START_REPLY, Step.SEND], to: Step.SEND },
        { from: [Step.START_REPLY, Step.SEND], to: Step.SUCCESS },

        // 错误处理：任何状态都可以转到 FAILED
        { from: [Step.ACCEPT, Step.START_REPLY, Step.SEND], to: Step.FAILED },

        // 结束
        { from: [Step.ACCEPT, Step.SUCCESS, Step.FAILED], to: Step.END },
    ];

    const stateMachine = new StateMachine<Step, ChatStateData>({
        initialState: Step.ACCEPT,
        transitions,
        onInvalidTransition: (from, to, error) => {
            console.error(`聊天状态机无效转换: ${error}`);
        },
        onStateChange: (from, to, context) => {
            console.debug(`聊天状态转换: ${from} -> ${to}`);
        },
    });

    // 注册状态处理器
    stateMachine
        .on(Step.ACCEPT, async (data) => {
            await callbacks.onAccept?.();
        })
        .on(
            Step.START_REPLY,
            async (data) => {
                await callbacks.onStartReply?.();
            },
            true,
        ) // START_REPLY 是必需等待的
        .on(Step.SEND, async (data) => {
            if (!callbacks.onSend) return;

            // 处理工具调用反馈的名称
            if (data.status_message) {
                await callbacks.onSend({
                    type: 'status',
                    content: data.status_message,
                });
            }

            // 处理思维链内容
            if (data.reason_content) {
                await callbacks.onSend({
                    type: 'think',
                    content: data.reason_content,
                });
            }

            // 处理回复内容
            if (data.content) {
                await callbacks.onSend({
                    type: 'text',
                    content: data.content,
                });
            }
        })
        .on(Step.SUCCESS, async (data) => {
            const content = `${data.content ?? ''}\n${data.reason_content ?? ''}`.trim();
            await callbacks.onSuccess?.(content);
        })
        .on(Step.FAILED, async (_) => {
            const error = new Error('聊天处理失败');
            await callbacks.onFailed?.(error);
        })
        .on(Step.END, async (_) => {
            await callbacks.onEnd?.();
        });

    return stateMachine;
}

/**
 * 聊天状态机管理器
 * 提供更高级的 API 来管理聊天状态
 */
export class ChatStateMachineManager {
    private stateMachine: StateMachine<Step, ChatStateData>;
    private isFinished = false;

    constructor(callbacks: ChatStateMachineCallbacks) {
        this.stateMachine = createChatStateMachine(callbacks);
    }

    /**
     * 处理聊天响应
     */
    async handleResponse(data: ChatStateData): Promise<boolean> {
        if (this.isFinished) {
            console.warn('聊天已结束，忽略后续响应');
            return false;
        }

        const success = await this.stateMachine.transition(data.step, data);

        // 检查是否结束
        if (data.step === Step.END || data.step === Step.FAILED) {
            this.isFinished = true;
        }

        return success;
    }

    /**
     * 等待指定状态完成
     */
    async waitForState(state: Step): Promise<void> {
        await this.stateMachine.waitForState(state);
    }

    /**
     * 等待所有状态完成
     */
    async waitForAll(): Promise<void> {
        await this.stateMachine.waitForAll();
    }

    /**
     * 获取当前状态
     */
    getCurrentState(): Step | null {
        return this.stateMachine.getCurrentState();
    }

    /**
     * 获取状态历史
     */
    getStateHistory(): Step[] {
        return this.stateMachine.getStateStack();
    }

    /**
     * 检查是否已完成
     */
    isComplete(): boolean {
        return this.isFinished;
    }

    /**
     * 手动结束（用于错误处理）
     */
    async forceEnd(error?: Error): Promise<void> {
        if (!this.isFinished) {
            if (error) {
                await this.stateMachine.transition(Step.FAILED, { step: Step.FAILED });
            }
            await this.stateMachine.transition(Step.END, { step: Step.END });
            this.isFinished = true;
        }
    }

    /**
     * 重置状态机
     */
    reset(): void {
        this.stateMachine.reset();
        this.isFinished = false;
    }
}
