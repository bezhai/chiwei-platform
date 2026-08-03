// 泳道绑定解析：一条入站消息属于哪个泳道。
//
// 注意与根导出的 LaneRouter 区分 —— 那个是服务发现（某服务的某泳道实例在哪个
// 地址），这里是绑定解析（这条消息归哪个泳道）。

export type { LaneBindingStore } from './resolver';
export { LaneBindingResolver } from './resolver';
export { TypeOrmLaneBindingStore } from './store';
export { getLaneBindingResolver } from './runtime';
export {
    LANE_ROUTE_TYPE,
    LANE_ROUTING_TABLE,
    LaneRouting,
    type LaneRouteType,
} from '../entities/lane-routing';
