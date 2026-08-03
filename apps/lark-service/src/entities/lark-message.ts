import { Column, CreateDateColumn, Entity, Index, PrimaryColumn } from 'typeorm';

/**
 * 飞书原生消息。与 common_message 的 user 行在同一个事务里写入 —— 这是唯一一处
 * 「common_* 与渠道私有表同事务」的写入，共库正是为了保住它。
 */
@Entity('lark_message')
@Index('uq_lark_message_common_message_id', ['common_message_id'], { unique: true })
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
