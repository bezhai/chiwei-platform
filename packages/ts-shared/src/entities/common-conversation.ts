import { Column, CreateDateColumn, Entity, Index, PrimaryColumn, UpdateDateColumn } from 'typeorm';

@Entity('common_conversation')
@Index('idx_common_conversation_channel_scope', ['channel', 'scope'])
export class CommonConversation {
    @PrimaryColumn({ type: 'uuid' })
    common_conversation_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 16 })
    scope!: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    display_name?: string;

    @Column({ type: 'text', nullable: true })
    avatar_url?: string;

    @Column({ type: 'integer', nullable: true })
    member_count?: number;

    @Column({ type: 'boolean', default: true })
    is_active!: boolean;

    @Column({ type: 'jsonb', nullable: true })
    attachment_policy?: Record<string, unknown>;

    @CreateDateColumn({ name: 'created_at', type: 'timestamp' })
    created_at!: Date;

    @UpdateDateColumn({ name: 'updated_at', type: 'timestamp' })
    updated_at!: Date;
}
