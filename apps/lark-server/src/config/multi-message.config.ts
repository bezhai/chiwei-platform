/**
 * 多消息回复配置
 */
export interface MultiMessageConfig {
    /** 消息分隔符，默认 '---split---' */
    splitMarker: string;
    /** 默认消息间隔（毫秒），默认 2500ms */
    defaultDelay: number;
    /** 最小消息间隔（毫秒），默认 1000ms */
    minDelay: number;
    /** 最大消息间隔（毫秒），默认 5000ms */
    maxDelay: number;
    /** 最大消息数量，超过后合并到最后一条，默认 4 */
    maxMessages: number;
}

/**
 * 默认多消息配置
 */
export const multiMessageConfig: MultiMessageConfig = {
    splitMarker: '---split---',
    defaultDelay: 2500,
    minDelay: 1000,
    maxDelay: 5000,
    maxMessages: 4,
};
