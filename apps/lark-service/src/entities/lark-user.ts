import { Column, Entity, PrimaryColumn } from 'typeorm';

/** 飞书用户在开放平台维度的身份（union_id 跨应用稳定，open_id 不是）。 */
@Entity('lark_user')
export class LarkUser {
    @PrimaryColumn()
    union_id!: string;

    @Column()
    name!: string;

    @Column({ type: 'text', nullable: true })
    avatar_origin?: string;

    @Column({ type: 'boolean', nullable: true })
    is_admin?: boolean;
}
