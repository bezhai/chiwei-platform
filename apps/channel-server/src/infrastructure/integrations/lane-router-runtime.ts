// 生产运行时的 LaneRouter 单例：core LaneRouter + TypeOrmLaneRoutingStore。
// 未来接线点（入站决策点、admin 改绑定后 clearCache）都用这一个实例，保证
// 泳道决策读的是同一份 30s 缓存、改绑定 clearCache 命中同一份缓存。
//
// 单测不 import 本文件（它静态依赖 TypeORM 数据源）；LaneRouter 单测注入内存
// FakeLaneRoutingStore。本文件只在运行时接线点被调用。

import { LaneRouter } from '@core/channels/lane-routing/lane-router';
import { TypeOrmLaneRoutingStore } from '@repositories/lane-routing-store';

let singleton: LaneRouter | null = null;

export function getLaneRouter(): LaneRouter {
    if (singleton === null) {
        singleton = new LaneRouter(new TypeOrmLaneRoutingStore());
    }
    return singleton;
}
