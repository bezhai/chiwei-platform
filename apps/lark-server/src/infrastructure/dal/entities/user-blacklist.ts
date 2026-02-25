import { Entity, PrimaryColumn, Column, CreateDateColumn } from 'typeorm';

@Entity('user_blacklist')
export class UserBlacklist {
    @PrimaryColumn({ type: 'varchar', length: 100 })
    union_id!: string; // 被封禁用户的 union_id

    @Column({ type: 'text', nullable: true })
    reason?: string; // 封��原因

    @Column({ type: 'varchar', length: 100, nullable: true })
    blocked_by?: string; // 操作人 union_id

    @CreateDateColumn()
    created_at!: Date; // 封禁时间
}
