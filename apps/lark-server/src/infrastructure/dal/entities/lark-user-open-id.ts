import { Entity, Column, PrimaryColumn } from 'typeorm';

@Entity('lark_user_open_id') // 表名
export class LarkUserOpenId {
    @PrimaryColumn({ name: 'app_id', type: 'varchar' })
    appId!: string; // 应用的唯一标识

    @PrimaryColumn({ name: 'open_id', type: 'varchar' })
    openId!: string; // 用户在该应用下的唯一标识

    @Column({ name: 'union_id', type: 'varchar', nullable: true })
    unionId?: string; // 用户在开放平台的唯一标识

    @Column({ type: 'varchar' })
    name!: string; // 用户名称
}
