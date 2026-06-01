import { Column, CreateDateColumn, Entity, PrimaryColumn, UpdateDateColumn } from 'typeorm';

@Entity('common_user')
export class CommonUser {
    @PrimaryColumn({ type: 'uuid' })
    common_user_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    display_name?: string;

    @Column({ type: 'text', nullable: true })
    avatar_url?: string;

    @CreateDateColumn({ name: 'created_at', type: 'timestamp' })
    created_at!: Date;

    @UpdateDateColumn({ name: 'updated_at', type: 'timestamp' })
    updated_at!: Date;
}
