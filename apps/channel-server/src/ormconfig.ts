// 本服务的数据库组装根：连接怎么建由 @inner/shared 提供，**注册哪些表由这里
// 决定**。共享包一条实体都不追加 —— 否则本服务会连带加载别的服务独占的表。
//
// 组装完立刻 bindDataSource：共享包里的通用能力（bot 身份目录、黑名单规则、
// chat.request 的 pending 行落库）要读写库，但它们不能反向 import 本文件。
// 由本组装根把 DataSource 递进去，共享包只从绑定处读。

import { bindDataSource, createPostgresDataSource } from '@inner/shared/persistence';
import {
    BotConfig,
    CommonAgentResponse,
    CommonBotPresence,
    CommonConversation,
    CommonMessage,
    CommonUser,
    LaneRouting,
    UserBlacklist,
} from '@inner/shared/entities';
import { QqUserOpenId, QqMessage, QqGroupChatInfo } from './infrastructure/dal/entities';

const AppDataSource = createPostgresDataSource({
    entities: [
        // 渠道无关的公共层（定义在 @inner/shared，由本服务选择注册）
        CommonUser,
        CommonConversation,
        CommonMessage,
        CommonAgentResponse,
        BotConfig,
        UserBlacklist,
        LaneRouting,
        CommonBotPresence,
        // 本服务自己的表
        QqUserOpenId,
        QqMessage,
        QqGroupChatInfo,
    ],
});

bindDataSource(AppDataSource);

export default AppDataSource;
