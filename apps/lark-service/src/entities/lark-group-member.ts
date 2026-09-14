import { Column, Entity, PrimaryColumn } from 'typeorm';

/** 群成员及其群内身份。目录写入时显式保存最近成员证据的 UTC 时间。 */
@Entity('lark_group_member')
export class LarkGroupMember {
    @PrimaryColumn()
    chat_id!: string;

    @PrimaryColumn()
    union_id!: string;

    @Column({ type: 'boolean', default: false })
    is_owner?: boolean;

    @Column({ type: 'boolean', default: false })
    is_manager?: boolean;

    @Column({ type: 'boolean', default: false })
    is_leave?: boolean;

    @Column({ type: 'timestamp', default: () => 'CURRENT_TIMESTAMP' })
    created_at?: Date;

    @Column({
        type: 'timestamp',
        default: () => 'CURRENT_TIMESTAMP',
    })
    updated_at!: Date;
}
