import { Column, CreateDateColumn, Entity, Index, PrimaryColumn } from 'typeorm';

// QQ 消息 id ↔ common_message 映射（对飞书 LarkMessage）。
//
// 入站存原始 QQ msg_id（被动回复要回带它），出站存网关发回的新 QQ msg_id。
// common_message_id 唯一索引：出站反查 common → QQ msg_id 走它。
@Entity('qq_message')
@Index('uq_qq_message_common_message_id', ['common_message_id'], { unique: true })
@Index('idx_qq_message_conversation_id', ['conversation_id'])
export class QqMessage {
    @PrimaryColumn({ name: 'qq_message_id', type: 'varchar', length: 256 })
    qq_message_id!: string;

    @Column({ type: 'uuid' })
    common_message_id!: string;

    @Column({ type: 'varchar', length: 256 })
    conversation_id!: string;

    @Column({ name: 'bot_name', type: 'varchar', length: 64, nullable: true })
    bot_name?: string;

    @Column({ type: 'varchar', length: 16, nullable: true })
    scope?: string;

    @Column({ name: 'sender_open_id', type: 'varchar', length: 256, nullable: true })
    sender_open_id?: string;

    @Column({ name: 'reply_qq_message_id', type: 'varchar', length: 256, nullable: true })
    reply_qq_message_id?: string;

    @Column({ type: 'jsonb', nullable: true })
    raw_event?: Record<string, unknown>;

    @CreateDateColumn({ name: 'created_at', type: 'timestamp' })
    created_at!: Date;
}
