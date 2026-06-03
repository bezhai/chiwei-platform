import { Entity, PrimaryColumn, Column, CreateDateColumn, UpdateDateColumn } from 'typeorm';

@Entity('bot_config')
export class BotConfig {
    @PrimaryColumn({ type: 'varchar', length: 50 })
    bot_name!: string; // 机器人名称，用作唯一标识

    // 接入渠道，不是写死枚举（"lark" / "qq" / 以后任意）。bot 加载链路按它分发
    // 到对应 channel 的 InboundAdapter / OutboundCapabilities / AddressingPolicy。
    @Column({ type: 'varchar', length: 20, default: 'lark' })
    channel!: string;

    // bot 在 common_user 里的身份。channel-server 启动加载 bot_config 时会为缺失
    // 的 bot 分配一个 common_user_id，并在整个 channel 生命周期内通过 BotConfig
    // 暴露。群聊 @bot 判断只比较 common user id，不再比较平台 union/open id。
    @Column({ name: 'common_user_id', type: 'uuid', nullable: true })
    common_user_id?: string;

    // 各 channel 自己的凭据结构，框架不约束 JSONB 形状（形状由各 adapter 解释）。
    //   lark: { app_id, app_secret, encrypt_key, verification_token, robot_union_id }
    //   qq:   { app_id, app_secret, bot_secret }
    // 飞书原来散在独立列里的凭据已一刀切迁进这里，旧列已删（不留双形态）。
    @Column({ type: 'jsonb', nullable: true })
    credentials?: Record<string, unknown> | null;

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
