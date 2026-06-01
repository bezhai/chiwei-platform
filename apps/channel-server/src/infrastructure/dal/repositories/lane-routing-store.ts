// 生产运行时的 LaneRoutingStore 实现：用 TypeORM 复用 channel-server 现有的
// AppDataSource（@chiwei 业务库，不新开 PG Pool）读 lane_routing 表。
//
// 查询语义与现行 lane_routing 数据口径一致：
//   SELECT lane_name FROM lane_routing
//   WHERE route_type = $type AND route_key = $key AND is_active = true LIMIT 1
// route_type 是字符串判别值（真实 @chiwei 库该列是 character varying，值
// 'bot' / 'chat' / 'group'）。
//
// 单测不 import 本文件（它静态依赖 TypeORM 数据源）；LaneRouter 的单测走内存版
// FakeLaneRoutingStore。本文件只在运行时接线时使用。

import AppDataSource from '@ormconfig';
import { LaneRouting } from '@entities/lane-routing';
import type { LaneRoutingStore } from '@core/channels/lane-routing/lane-router';

// route_type 是字符串判别值，生产写法固定为 'chat' / 'bot'。
const CHAT_ROUTE_TYPE = 'chat';
const BOT_ROUTE_TYPE = 'bot';

export class TypeOrmLaneRoutingStore implements LaneRoutingStore {
    async findChatLane(commonConversationId: string): Promise<string | null> {
        return this.findLane(CHAT_ROUTE_TYPE, commonConversationId);
    }

    async findBotLane(botGlobalId: string): Promise<string | null> {
        return this.findLane(BOT_ROUTE_TYPE, botGlobalId);
    }

    private async findLane(routeType: string, routeKey: string): Promise<string | null> {
        const repo = AppDataSource.getRepository(LaneRouting);
        const row = await repo.findOne({
            where: {
                route_type: routeType,
                route_key: routeKey,
                is_active: true,
            },
        });
        return row ? row.lane_name : null;
    }
}
