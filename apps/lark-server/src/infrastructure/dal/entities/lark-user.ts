import { Entity, PrimaryColumn, Column } from 'typeorm';

@Entity('lark_user')
export class LarkUser {
    @PrimaryColumn()
    union_id!: string; // 用户唯一标识

    @Column()
    name!: string; // 用户名称

    @Column({ type: 'text', nullable: true })
    avatar_origin?: string; // 用户头像 URL

    @Column({ type: 'boolean', nullable: true })
    is_admin?: boolean; // 是否超级管理员
}
