// 本服务独占的表。渠道无关的公共层（common_* / bot_config / user_blacklist）
// 定义在 @inner/shared/entities，不在这里重复一份。

export * from './lark-emoji';
export * from './lark-user';
export * from './lark-group-member';
export * from './lark-base-chat-info';
export * from './lark-group-chat-info';

export * from './user-group-binding';
export * from './lark-user-open-id';

export * from './bot-persona';

export * from './lark-message';

export * from './qq-user-open-id';
export * from './qq-message';
export * from './qq-group-chat-info';
