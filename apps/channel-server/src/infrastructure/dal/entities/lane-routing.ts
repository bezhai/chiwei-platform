import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

// lane_routing：泳道路由绑定表（@chiwei 业务库）。一行 = 「某个绑定对象当前路由到
// 哪个 lane」。channel-server 读取 bot 维度（route_type='bot'）做泳道决策；
// lane-bindings admin API 写入同一张表并主动失效本进程缓存。
//
// route_type 是字符串判别值（真实 @chiwei 库该列是 character varying）：
//   'bot'   = 按全局 bot 标识绑定（本期唯一读取的维度）
//   'chat'  = 按会话绑定（本期不读、不参与决策）
//   'group' = 按群绑定（本期不读、不参与决策）
// route_key 在 bot 维度存全局 bot 标识；lane_name 是目标泳道（prod 表示默认）；
// is_active 是软删除标记。
@Entity('lane_routing')
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
