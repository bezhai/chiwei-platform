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

    /**
     * 向量化状态
     * - pending: 待处理
     * - completed: 已完成
     * - failed: 失败
     */
    @Column({ length: 20, default: 'pending' })
    vector_status!: string;

    /**
     * 机器人名称（用于多 bot 场景下载图片等）
     */
    @Column({ length: 50, nullable: true })
    bot_name?: string;
}
