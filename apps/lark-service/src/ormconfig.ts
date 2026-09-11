// 本服务的数据库组装根：连接怎么建由 @inner/shared 提供，**注册哪些表由这里
// 决定**。共享包一条实体都不追加 —— 否则飞书进程会连带加载 QQ 独占的表。
//
// 惰性构造而不是模块体直接 new：import 一个模块不该读 env、不该建连接池、更不该
// 起定时器。真正取用时才组装，测试也就能在取用前决定 env 长什么样。
//
// 取用即保证已绑定：共享包里的通用能力（bot 身份目录、黑名单规则、泳道绑定解析）
// 要读写库，但它们不能反向 import 本文件，只能从 bindDataSource 绑定处读。
// 同实例重复绑定是幂等的。

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
import type { DataSource } from 'typeorm';

import { LARK_ENTITIES } from './entities';

export const LARK_SERVICE_ENTITIES: Function[] = [
    // 渠道无关的公共层（定义在 @inner/shared，由本服务选择注册）
    BotConfig,
    CommonAgentResponse,
    CommonBotPresence,
    CommonConversation,
    CommonMessage,
    CommonUser,
    LaneRouting,
    UserBlacklist,
    // 飞书独占
    ...LARK_ENTITIES,
];

let dataSource: DataSource | undefined;

export function larkDataSource(): DataSource {
    if (!dataSource) {
        dataSource = createPostgresDataSource({ entities: LARK_SERVICE_ENTITIES });
    }
    bindDataSource(dataSource);
    return dataSource;
}
