// 进程级的泳道绑定解析器单例：LaneBindingResolver + TypeORM store。
//
// 必须是单例：入站决策点和「改绑定后 clearCache」如果拿到不同实例，就会出现
// 改完绑定、缓存没被清、决策继续读旧值最多 30s 的情况。一个进程一份缓存。
//
// 这里没有任何服务专属输入（不像服务发现那个 LaneRouter 要吃调用方的 prom
// Registry），所以整套装配放在包内，两个服务直接取用即可。

import { LaneBindingResolver } from './resolver';
import { TypeOrmLaneBindingStore } from './store';

let singleton: LaneBindingResolver | null = null;

export function getLaneBindingResolver(): LaneBindingResolver {
    if (singleton === null) {
        singleton = new LaneBindingResolver(new TypeOrmLaneBindingStore());
    }
    return singleton;
}
