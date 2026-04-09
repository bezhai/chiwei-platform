import { Entity, PrimaryColumn, Column, CreateDateColumn, UpdateDateColumn } from 'typeorm';

@Entity('bot_config')
export class BotConfig {
    @PrimaryColumn({ type: 'varchar', length: 50 })
    bot_name!: string; // 机器人名称，用作唯一标识

    @Column({ type: 'varchar', length: 100 })
    app_id!: string; // 飞书应用ID

    @Column({ type: 'varchar', length: 200 })
    app_secret!: string; // 飞书应用密钥

    @Column({ type: 'varchar', length: 100 })
    encrypt_key!: string; // 加密密钥

    @Column({ type: 'varchar', length: 100 })
    verification_token!: string; // 验证令牌

    @Column({ type: 'varchar', length: 100 })
    robot_union_id!: string; // 机器人Union ID

    @Column({ type: 'varchar', length: 20, default: 'http' })
    init_type!: 'http' | 'websocket'; // 初始化类型：http或websocket

    @Column({ type: 'boolean', default: true })
    is_active!: boolean; // 是否启用

    @Column({ type: 'text', nullable: true })
    description?: string; // 机器人描述

    @Column({ type: 'boolean', default: false })
    is_dev!: boolean; // 是否为开发环境机器人

    @Column({ type: 'varchar', length: 20, default: 'persona' })
    bot_role!: 'persona' | 'utility'; // persona=拟人聊天, utility=工具功能

    @Column({ type: 'varchar', length: 50, nullable: true })
    persona_id?: string; // 关联 bot_persona.persona_id

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;

    @UpdateDateColumn({ name: 'updated_at' })
    updatedAt!: Date;
}
