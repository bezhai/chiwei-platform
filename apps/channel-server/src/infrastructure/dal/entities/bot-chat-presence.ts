import { Entity, PrimaryColumn, Column } from 'typeorm';

@Entity('bot_chat_presence')
export class BotChatPresence {
    @PrimaryColumn()
    chat_id!: string;

    @PrimaryColumn()
    bot_name!: string;

    @Column({ type: 'boolean', default: true })
    is_active!: boolean;

    @Column({ type: 'timestamptz', default: () => 'now()' })
    created_at!: Date;

    @Column({ type: 'timestamptz', default: () => 'now()' })
    updated_at!: Date;
}
