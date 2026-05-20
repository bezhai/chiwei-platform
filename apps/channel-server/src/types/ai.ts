// 基础 Message 接口

// System Message 接口
export interface SystemMessage {
    role: 'system'; // 固定为 "system"
    content: string; // 消息内容
    name?: string; // 名称，可选
}

interface TextMessage {
    type: 'text'; // 消息的类型
    text: string; // 消息的文本内容
}

interface ImageMessage {
    type: 'image_url'; // 消息的类型
    image_url: {
        url: string; // 图片的 URL
        detail?: string; // 图片的详细描述
    };
}

export type UserContent = TextMessage | ImageMessage;

// User Message 接口
export interface UserMessage {
    role: 'user'; // 固定为 "user"
    content: string | UserContent[]; // 消息内容
    name?: string; // 名称，可选
}

// Assistant Message 接口
export interface AssistantMessage {
    role: 'assistant'; // 固定为 "assistant"
    content: string; // 消息内容
    name?: string; // 名称，可选
}

// 通用的 Message 数组接口
export type Message = SystemMessage | UserMessage | AssistantMessage;

// 请求体的接口
export interface CompletionRequest {
    model: string; // 模型名称，例如 "qwen-plus"
    messages: Message[]; // 消息数组
    temperature?: number; // 温度参数，控制生成文本的随机性
    presence_penalty?: number; // 控制模型生成的多样性
    frequency_penalty?: number; // 控制模型生成的多样性
    stream?: boolean; // 是否流式传输
    stream_options?: {
        include_usage?: boolean; // 是否包含 Token 消耗信息
    };
    top_p?: number; // 采样概率，控制生成文本的多样性
    max_tokens?: number; // 最大 Token 数，控制生成文本的长度
    extra_body?: {
        [key: string]: any; // 其他自定义参数
    };
}

// Delta 对象，表示流式返回的部分内容
// 函数调用相关类型
export interface FunctionCall {
    name: string;
    arguments: string;
}

// 流式响应动作类型
export type StreamAction = 
    | { type: 'text'; content: string } 
    | { type: 'think'; content: string }
    | { type: 'status'; content: string };

export interface Delta {
    content?: string; // 生成的部分内容
    function_call?: FunctionCall; // 如果有函数调用
    refusal?: any; // 是否拒绝响应
    role?: string; // 角色信息，通常为 "assistant"
    tool_calls?: any; // 工具调用信息
}

// Choice 对象，表示每次生成的内容的一部分
export interface Choice {
    index: number;
    delta: Delta; // 流式返回的部分内容
    finish_reason: string | null; // 生成结束的原因 (如 "stop" 或 null)
    logprobs?: any; // 概率日志，通常为 null
}

// 流式响应接口
export interface StreamedCompletionChunk {
    id: string; // 请求的 ID
    created: number; // 创建时间戳
    model: string; // 使用的模型名称
    object: 'chat.completion.chunk'; // 对象类型，流式为 "chat.completion.chunk"
    choices: Choice[]; // 每次生成的内容
    service_tier?: string | null; // 服务等级，通常为 null
    system_fingerprint?: string | null; // 系统指纹，通常为 null
    usage?: any; // 使用情况，通常为 null
    message?: TempMessage; // 有些模型没有choices, 临时用这个
    citations?: string[]; // 引用信息，字符串数组形式
}

export interface TempMessage {
    content: string;
}

// 非流式响应中的完整 Message 对象
export interface RespMessage {
    content: string; // 完整的生成内容
    refusal?: any; // 是否拒绝响应
    role: string; // 角色信息，通常为 "assistant"
    audio?: any; // 音频信息，通常为 null
    function_call?: any; // 函数调用信息
    tool_calls?: any; // 工具调用信息
}

// 非流式响应中的 Choice 对象
export interface NonStreamChoice {
    index: number;
    message: RespMessage; // 完整的生成内容
    finish_reason: string; // 生成结束的原因 ("stop" 等)
    logprobs?: any; // 概率日志，通常为 null
}

// 非流式响应接口
export interface NonStreamedCompletion {
    id: string; // 请求的 ID
    created: number; // 创建时间戳
    model: string; // 使用的模型名称
    object: 'chat.completion'; // 对象类型，非流式为 "chat.completion"
    choices: NonStreamChoice[]; // 包含完整消息的 choice 数组
    service_tier?: string | null; // 服务等级，通常为 null
    system_fingerprint?: string | null; // 系统指纹，通常为 null
    usage: {
        completion_tokens: number; // 生成内容中的 token 数
        prompt_tokens: number; // 提示中的 token 数
        total_tokens: number; // 总 token 数
        completion_tokens_details?: any; // 生成的 token 详细信息，通常为 null
        prompt_tokens_details?: any; // 提示的 token 详细信息，通常为 null
    };
}

// 一个联合类型，可以表示流式或非流式的响应
export type CompletionResponse = StreamedCompletionChunk | NonStreamedCompletion;

export interface AnswerBox {
    snippet: string;
}

export interface OrganicResult {
    title: string;
    link: string;
    snippet: string;
}

export interface WebSearchResult {
    answer_box?: AnswerBox | null;
    organic_results: OrganicResult[];
}

export interface SearchWithAIResponse {
    keywords: string[];
    need_search: boolean;
    search: WebSearchResult | null;
}
