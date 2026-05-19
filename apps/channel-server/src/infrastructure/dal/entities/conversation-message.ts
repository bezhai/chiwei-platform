import { Entity, PrimaryColumn, Column } from 'typeorm';

/**
 * 会话消息实体
 * 用于存储用户和机器人的对话消息
 */
@Entity('conversation_messages')
export class ConversationMessage {
    @PrimaryColumn({ length: 100 })
    message_id!: string;

    @Column({ length: 100 })
    user_id!: string;

    /**
     * 发送者显示名冗余列。身份全局化后 user_id 是全局 internal_user_id，
     * 读取端不再 JOIN lark_user 取名，改读本列。历史数据迁移前为空。
     */
    @Column({ length: 100, nullable: true })
    username?: string;

    @Column({ type: 'text' })
    content!: string;

    @Column({ length: 20 })
    role!: string;

    @Column({ length: 100 })
    root_message_id!: string;

    @Column({ length: 100, nullable: true })
    reply_message_id?: string;

    @Column({ length: 100 })
    chat_id!: string;

    @Column({ length: 10 })
    chat_type!: string;

    @Column({ type: 'bigint' })
    create_time!: string;

    @Column({ length: 30, nullable: true, default: 'text' })
    message_type?: string;

    @Column({ length: 50, nullable: true })
    bot_name?: string;

    /** 关联 agent_responses.session_id（assistant 消息） */
    @Column({ length: 100, nullable: true })
    response_id?: string;
}
