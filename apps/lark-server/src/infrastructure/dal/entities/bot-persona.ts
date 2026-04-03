import {
    Column,
    CreateDateColumn,
    Entity,
    PrimaryColumn,
    UpdateDateColumn,
} from 'typeorm'

@Entity('bot_persona')
export class BotPersona {
    @PrimaryColumn({ type: 'varchar', length: 50 })
    bot_name!: string

    @Column({ type: 'varchar', length: 50 })
    display_name!: string

    @Column({ type: 'text' })
    persona_core!: string

    @Column({ type: 'text' })
    persona_lite!: string

    @Column({ type: 'text' })
    default_reply_style!: string

    @Column({ type: 'jsonb', default: '{}' })
    error_messages!: Record<string, string>

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date

    @UpdateDateColumn({ name: 'updated_at' })
    updatedAt!: Date
}
