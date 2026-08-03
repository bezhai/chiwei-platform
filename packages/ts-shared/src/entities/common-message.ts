import { Column, CreateDateColumn, Entity, Index, PrimaryColumn } from 'typeorm';

export interface CommonMessageContent {
    type: string;
    [key: string]: unknown;
}

@Entity('common_message')
@Index('idx_common_message_conversation_time', ['common_conversation_id', 'event_time'])
@Index('idx_common_message_user_time', ['common_user_id', 'event_time'])
@Index('idx_common_message_response_id', ['response_id'])
export class CommonMessage {
    @PrimaryColumn({ type: 'uuid' })
    common_message_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'uuid' })
    common_conversation_id!: string;

    @Column({ type: 'uuid', nullable: true })
    common_user_id?: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    sender_display_name?: string;

    @Column({ type: 'varchar', length: 20 })
    role!: string;

    @Column({ type: 'jsonb', default: () => "'[]'::jsonb" })
    content!: CommonMessageContent[];

    @Column({ type: 'text', nullable: true })
    content_text?: string;

    @Column({ type: 'uuid', nullable: true })
    common_root_message_id?: string;

    @Column({ type: 'uuid', nullable: true })
    common_reply_message_id?: string;

    @Column({ type: 'varchar', length: 16 })
    scope!: string;

    @Column({ type: 'varchar', length: 30, nullable: true })
    message_type?: string;

    @Column({ type: 'varchar', length: 50, nullable: true })
    bot_name?: string;

    @Column({ type: 'varchar', length: 100, nullable: true })
    response_id?: string;

    @Column({ type: 'bigint' })
    event_time!: string;

    @CreateDateColumn({ name: 'created_at', type: 'timestamp' })
    created_at!: Date;
}
