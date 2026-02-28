import { Entity, PrimaryColumn, Column } from 'typeorm';

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
