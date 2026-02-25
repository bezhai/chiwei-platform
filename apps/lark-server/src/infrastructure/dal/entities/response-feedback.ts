import { Entity, PrimaryColumn, Column, CreateDateColumn } from 'typeorm';

@Entity('response_feedback')
export class ResponseFeedback {
    @PrimaryColumn()
    id!: number; // 自增主键

    @Column()
    message_id!: string; // 机器人消息id

    @Column()
    chat_id!: string; // 群聊id

    @Column()
    parent_message_id!: string; // 用户消息id

    @Column()
    feedback_type!: string; // 反馈类型

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;
}
