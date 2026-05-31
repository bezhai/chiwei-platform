// 生产运行时的 LaneRoutingStore 实现：用 TypeORM 复用 channel-server 现有的
// AppDataSource（@chiwei 业务库，不新开 PG Pool）读 lane_routing 表。本期只读
// bot 维度（route_type='bot'），满足 core LaneRouter 钉死的结构型契约。
//
// 查询语义与现状 channel-proxy lane-resolver.ts 一致：
//   SELECT lane_name FROM lane_routing
//   WHERE route_type = 'bot' AND route_key = $botGlobalId AND is_active = true LIMIT 1
// route_type 是字符串判别值（真实 @chiwei 库该列是 character varying，值
// 'bot' / 'chat' / 'group'）。
//
// 单测不 import 本文件（它静态依赖 TypeORM 数据源）；LaneRouter 的单测走内存版
// FakeLaneRoutingStore。本文件只在运行时接线时使用。

import AppDataSource from '@ormconfig';
import { LaneRouting } from '@entities/lane-routing';
import type { LaneRoutingStore } from '@core/channels/lane-routing/lane-router';

// 本期只读 bot 维度。route_type 是字符串判别值，与现状 channel-proxy
// lane-resolver.ts 生产写法同口径（WHERE route_type = 'bot'）。
const BOT_ROUTE_TYPE = 'bot';

export class TypeOrmLaneRoutingStore implements LaneRoutingStore {
    async findBotLane(botGlobalId: string): Promise<string | null> {
        const repo = AppDataSource.getRepository(LaneRouting);
        const row = await repo.findOne({
            where: {
                route_type: BOT_ROUTE_TYPE,
                route_key: botGlobalId,
                is_active: true,
            },
        });
        return row ? row.lane_name : null;
    }
}
