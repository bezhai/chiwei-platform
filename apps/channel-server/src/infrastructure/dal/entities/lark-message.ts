import { Column, CreateDateColumn, Entity, Index, PrimaryColumn } from 'typeorm';

@Entity('lark_message')
@Index('uq_lark_message_common_message_id', ['common_message_id'], {
    unique: true,
})
@Index('idx_lark_message_chat_id', ['chat_id'])
export class LarkMessage {
    @PrimaryColumn({ type: 'varchar', length: 256 })
    om_id!: string;

    @Column({ type: 'uuid' })
    common_message_id!: string;

    @Column({ type: 'varchar', length: 256 })
    chat_id!: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    sender_open_id?: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    sender_union_id?: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    root_om_id?: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    reply_om_id?: string;

    @Column({ type: 'varchar', length: 64 })
    message_type!: string;

    @Column({ type: 'jsonb', nullable: true })
    raw_event?: Record<string, unknown>;

    @CreateDateColumn({ name: 'created_at', type: 'timestamp' })
    created_at!: Date;
}
