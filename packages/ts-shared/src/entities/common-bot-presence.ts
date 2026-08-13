import { Column, Entity, Index, PrimaryColumn } from 'typeorm';

@Entity('common_bot_presence')
@Index('idx_common_bot_presence_bot', ['bot_name'])
export class CommonBotPresence {
    @PrimaryColumn({ type: 'uuid' })
    common_conversation_id!: string;

    @PrimaryColumn({ type: 'varchar', length: 50 })
    bot_name!: string;

    @Column({ type: 'boolean', default: true })
    is_active!: boolean;

    @Column({ type: 'timestamptz', default: () => 'now()' })
    created_at!: Date;

    @Column({ type: 'timestamptz', default: () => 'now()' })
    updated_at!: Date;
}
