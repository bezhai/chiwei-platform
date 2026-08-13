// 共享能力自己要读写的表的仓储入口。
//
// 全部是函数而不是模块级常量：常量意味着 import 期就 getRepository，那时
// DataSource 还没被组装根绑上（而且实体元数据也还没齐）。函数化后 import 期
// 零副作用，真正用到时才解析。
//
// 这里只出现共享能力自己会碰的表。各渠道私有表的仓储归各自服务，绝不进来。

import type { Repository } from 'typeorm';

import { BotConfig } from '../entities/bot-config';
import { CommonAgentResponse } from '../entities/common-agent-response';
import { CommonUser } from '../entities/common-user';
import { UserBlacklist } from '../entities/user-blacklist';
import { repositoryFor } from './data-source';

export function botConfigRepo(): Repository<BotConfig> {
    return repositoryFor(BotConfig);
}

export function commonUserRepo(): Repository<CommonUser> {
    return repositoryFor(CommonUser);
}

export function commonAgentResponseRepo(): Repository<CommonAgentResponse> {
    return repositoryFor(CommonAgentResponse);
}

export function userBlacklistRepo(): Repository<UserBlacklist> {
    return repositoryFor(UserBlacklist);
}
