// 飞书独占的八张表。渠道无关的公共层（common_* / bot_config / user_blacklist /
// lane_routing）定义在 @inner/shared/entities，不在这里重复一份。
//
// 「飞书独占」的判据是**全仓的读写点有没有一处不是飞书**，不是表名有没有 lark 前缀
// —— `user_group_binding` 就没有，但它只被 `/bind`、`/unbind` 和退群自动拉回读写，
// 列里存的是飞书的 union_id 和 chat_id。
//
// LARK_ENTITIES 是这八张表的唯一清单：ormconfig 拿它拼实体全集，schema.test 拿它
// 逐字段核对物理表。两处引用同一个数组，不会出现"注册了但没被核对"的表。
//
// 反过来，**本服务读得到但不属于飞书的表不进这份清单**（`bot_persona` 就是这样，
// 见 lark/persona-names.ts）：多塞一张，这个数组就不再是"飞书独占"那个意思了。

import { LarkBaseChatInfo } from './lark-base-chat-info';
import { LarkEmoji } from './lark-emoji';
import { LarkGroupChatInfo } from './lark-group-chat-info';
import { LarkGroupMember } from './lark-group-member';
import { LarkMessage } from './lark-message';
import { LarkUser } from './lark-user';
import { LarkUserOpenId } from './lark-user-open-id';
import { UserGroupBinding } from './user-group-binding';

export {
    LarkBaseChatInfo,
    LarkEmoji,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkMessage,
    LarkUser,
    LarkUserOpenId,
    UserGroupBinding,
};

export const LARK_ENTITIES = [
    LarkBaseChatInfo,
    LarkEmoji,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkMessage,
    LarkUser,
    LarkUserOpenId,
    UserGroupBinding,
] as const;
