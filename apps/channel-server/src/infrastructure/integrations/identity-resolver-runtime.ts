// 生产运行时的 IdentityResolver 单例：DbIdentityResolver + TypeOrmIdentityStore
// （5a 已实现，本步只接线、不改其内部）。inbound-pipeline /
// outbound-pipeline / storeMessage / chat-response-worker 在真实链路里都用
// 这一个实例，保证三类身份映射读写走同一张表、同一套 ON CONFLICT 收敛语义。
//
// 单测不 import 本文件（它静态依赖 TypeORM 数据源）；pipeline 单测注入
// InMemoryIdentityResolver。本文件只在运行时接线点被调用。

import { DbIdentityResolver } from '@core/channels/db-identity-resolver';
import type { IdentityResolver } from '@core/channels/identity-resolver';
import { TypeOrmIdentityStore } from '@repositories/identity-store';

let singleton: IdentityResolver | null = null;

export function getIdentityResolver(): IdentityResolver {
    if (singleton === null) {
        singleton = new DbIdentityResolver(new TypeOrmIdentityStore());
    }
    return singleton;
}
