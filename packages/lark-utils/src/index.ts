// Client
export { LarkClient, getLarkClient, resetLarkClient, createLarkClient } from './client';

// Manager
export { LarkClientManager, getLarkClientManager } from './manager';

// Types
//
// 类型和值必须分开导出。合在一个 `export { ... }` 里的话，逐模块转译的运行时
// （bun run / bun test）看不到 './types' 的类型声明，只会照字面去找同名的运行时导出，
// 找不到就抛 "export 'X' not found"。打包器（bun build）能消掉这层，所以症状是
// **编译出来的二进制正常、直接跑源码就崩**。
export type {
    // Config
    LarkClientConfig,
    // Response types
    LarkResponse,
    // Message types
    MessageType,
    ReceiveIdType,
    SendMessageParams,
    ReplyMessageParams,
    GetMessageListParams,
    // Entity types
    ChatMember,
    ChatInfo,
    UserInfo,
    MessageInfo,
} from './types';

export {
    createDefaultLarkConfig,
    ERROR_CODE_MAP,
    // 飞书的数字错误码：谁抛的、怎么问。业务层按码分支要用（见 client.ts 的
    // handleResponse）。
    LARK_MESSAGE_ALREADY_RECALLED,
    larkErrorCode,
    withLarkErrorCode,
} from './types';
