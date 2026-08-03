// LaneBindingStore 的运行时实现：走本进程已绑定的 DataSource 读 lane_routing 表。
//
// 查询语义：
//   SELECT lane_name FROM lane_routing
//   WHERE route_type = $type AND route_key = $key AND is_active = true LIMIT 1
//
// 不自己开连接池、也不 import 任何服务的 ormconfig（那是反向依赖）：DataSource
// 由各服务的组装根 bindDataSource 递进来。
//
// LaneBindingResolver 的单测不 import 本文件（它依赖 TypeORM 数据源），走内存
// fake store；本文件自己的测试只钉死发出去的查询条件（wire 契约）。

import { LaneRouting, LANE_ROUTE_TYPE } from '../entities/lane-routing';
import { repositoryFor } from '../persistence/data-source';
import type { LaneBindingStore } from './resolver';

export class TypeOrmLaneBindingStore implements LaneBindingStore {
    async findChatLane(commonConversationId: string): Promise<string | null> {
        return this.findLane(LANE_ROUTE_TYPE.chat, commonConversationId);
    }

    async findBotLane(botGlobalId: string): Promise<string | null> {
        return this.findLane(LANE_ROUTE_TYPE.bot, botGlobalId);
    }

    private async findLane(routeType: string, routeKey: string): Promise<string | null> {
        const row = await repositoryFor(LaneRouting).findOne({
            where: {
                route_type: routeType,
                route_key: routeKey,
                is_active: true,
            },
        });
        return row ? row.lane_name : null;
    }
}
