import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

// lane_routing：泳道绑定表。一行 = 「某个绑定对象当前路由到哪个 lane」。
// 读侧（LaneBindingResolver）按 route_type 查绑定做入站泳道决策；写侧的
// lane-bindings admin API 写同一张表并主动失效读侧缓存。
//
// 表名与 route_type 取值是**读写两侧共用的契约**，所以在这里定义一次：读侧走
// 实体 + 常量，写侧的裸 SQL 也引用同一个表名常量，避免两边各写一份字面量、
// 改一处漏一处。

/** 表名。读侧实体与写侧裸 SQL 共用这一个定义。 */
export const LANE_ROUTING_TABLE = 'lane_routing';

/**
 * route_type 的取值。真实库里该列是 character varying，值是小写字符串 ——
 * 曾经有一版把它误编码成整数枚举，查 route_type='1' 永远命不中真实行（真实值
 * 是 'bot'），所有 bot 维度绑定静默失效全部 fallback 到 prod。这里定成常量，
 * 并由 store 的 wire 契约测试钉死。
 */
export const LANE_ROUTE_TYPE = {
    /** 按会话绑定，route_key 存 common_conversation_id */
    chat: 'chat',
    /** 按全局 bot 标识绑定，route_key 存 bot 标识 */
    bot: 'bot',
} as const;

export type LaneRouteType = (typeof LANE_ROUTE_TYPE)[keyof typeof LANE_ROUTE_TYPE];

// route_key 按 route_type 存对应维度的 common 口径标识；lane_name 是目标泳道
//（prod 表示默认）；is_active 是软删除标记。
@Entity(LANE_ROUTING_TABLE)
export class LaneRouting {
    @PrimaryGeneratedColumn({ type: 'bigint' })
    id!: string;

    @Column({ type: 'varchar' })
    route_type!: string;

    @Column({ type: 'varchar' })
    route_key!: string;

    @Column({ type: 'varchar' })
    lane_name!: string;

    @Column({ type: 'boolean', default: true })
    is_active!: boolean;
}
