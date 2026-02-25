import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, UpdateDateColumn } from 'typeorm';

export interface AgentResponseReply {
    message_id: string;
    content_type?: string;
    sent_at: string;
}

export interface SafetyResult {
    reason?: string;
    detail?: string;
    confidence?: number;
    recalled?: number;
    failed?: number;
    checked_at?: string;
}

@Entity('agent_responses')
export class AgentResponse {
    @PrimaryGeneratedColumn('uuid')
    id!: string;

    @Column({ type: 'varchar', length: 100, unique: true })
    session_id!: string;

    @Column({ type: 'varchar', length: 100 })
    trigger_message_id!: string;

    @Column({ type: 'varchar', length: 100 })
    chat_id!: string;

    @Column({ type: 'varchar', length: 50, nullable: true })
    bot_name?: string;

    @Column({ type: 'varchar', length: 30, default: 'reply' })
    response_type!: string;

    @Column({ type: 'jsonb', default: '[]' })
    replies!: AgentResponseReply[];

    @Column({ type: 'text', nullable: true })
    response_text?: string;

    @Column({ type: 'jsonb', default: '{}' })
    agent_metadata!: Record<string, unknown>;

    @Column({ type: 'varchar', length: 20, default: 'pending' })
    safety_status!: string;

    @Column({ type: 'jsonb', nullable: true })
    safety_result?: SafetyResult;

    @Column({ type: 'varchar', length: 20, default: 'created' })
    status!: string;

    @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
    created_at!: Date;

    @UpdateDateColumn({ name: 'updated_at', type: 'timestamptz' })
    updated_at!: Date;
}
