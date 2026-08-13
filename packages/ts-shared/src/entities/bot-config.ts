import { Entity, PrimaryColumn, Column, CreateDateColumn, UpdateDateColumn } from 'typeorm';

@Entity('bot_config')
export class BotConfig {
    @PrimaryColumn({ type: 'varchar', length: 50 })
    bot_name!: string; // 机器人名称，用作唯一标识

    // 这个 bot 接在哪个渠道上。刻意是自由字符串而不是枚举：本包不认识任何具体
    // 渠道，各服务按这一列决定"这个 bot 归不归我管"（BotDirectory.load 的 channel
    // 过滤），以及交给哪个 ChannelPlugin 处理。
    // 列默认值定义在数据库 schema 里，不在这里复述 —— 复述一份就是把某个具体渠道
    // 的名字焊进共享包。
    @Column({ type: 'varchar', length: 20 })
    channel!: string;

    // bot 在 common_user 里的身份。服务启动加载 bot_config 时会为缺失的 bot 分配
    // 一个 common_user_id，并在整个生命周期内通过 BotConfig 暴露。群聊里"这条消息
    // 是不是冲 bot 来的"只比较 common user id，不比较任何渠道裸 id。
    @Column({ name: 'common_user_id', type: 'uuid', nullable: true })
    common_user_id?: string;

    // 各渠道自己的凭据，一团不透明 JSONB。**本包不解释它的形状** —— 形状由各渠道
    // 的 ChannelPlugin.parseCredentials 解释，共享包连字段名都不该知道。
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
