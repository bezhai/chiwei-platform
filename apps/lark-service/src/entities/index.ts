// 飞书独占的七张表。渠道无关的公共层（common_* / bot_config / user_blacklist /
// lane_routing）定义在 @inner/shared/entities，不在这里重复一份。
//
// LARK_ENTITIES 是这七张表的唯一清单：ormconfig 拿它拼实体全集，schema.test 拿它
// 逐字段核对物理表。两处引用同一个数组，不会出现"注册了但没被核对"的表。

import { LarkBaseChatInfo } from './lark-base-chat-info';
import { LarkEmoji } from './lark-emoji';
import { LarkGroupChatInfo } from './lark-group-chat-info';
import { LarkGroupMember } from './lark-group-member';
import { LarkMessage } from './lark-message';
import { LarkUser } from './lark-user';
import { LarkUserOpenId } from './lark-user-open-id';

export {
    LarkBaseChatInfo,
    LarkEmoji,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkMessage,
    LarkUser,
    LarkUserOpenId,
};

export const LARK_ENTITIES = [
    LarkBaseChatInfo,
    LarkEmoji,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkMessage,
    LarkUser,
    LarkUserOpenId,
] as const;
