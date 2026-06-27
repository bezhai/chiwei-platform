import { Entity, Column, Index, PrimaryColumn } from 'typeorm';

// QQ openid ↔ common_user 映射（对飞书 LarkUserOpenId）。
//
// QQ 的 openid 是 per-bot 的，且私聊 user_openid 与群 member_openid 是两个不同的
// ID 空间、member_openid 还跨群变化（见 docs/plan/qq-channel-integration.md non-goal）。
// 为了「私聊 user_openid、群 member_openid 各自稳定归一、互不混淆」，主键里用
// scope_key 把作用域折进身份：
//   - 私聊：scope_key = 'direct'，身份就是 (bot, user_openid)，跨会话稳定（owner 锚定靠它）。
//   - 群聊：scope_key = 'group:<群会话id>'，身份是 (bot, 群, member_openid)，
//           既不会和私聊撞、也不会跨群把两个人并成一个。
@Entity('qq_user_open_id')
@Index('idx_qq_user_open_id_common_user_id', ['commonUserId'])
export class QqUserOpenId {
    @PrimaryColumn({ name: 'bot_name', type: 'varchar', length: 64 })
    botName!: string;

    @PrimaryColumn({ name: 'scope_key', type: 'varchar', length: 128 })
    scopeKey!: string; // 'direct' | 'group:<conversation_id>'

    @PrimaryColumn({ name: 'open_id', type: 'varchar', length: 256 })
    openId!: string; // 私聊 user_openid / 群 member_openid

    @Column({ name: 'scope', type: 'varchar', length: 16 })
    scope!: string; // 'direct' | 'group'（冗余于 scope_key，便于查询/观测）

    @Column({ name: 'conversation_id', type: 'varchar', length: 256, nullable: true })
    conversationId?: string; // 实际会话裸 id（观测用）

    @Column({ type: 'varchar', length: 256, nullable: true })
    name?: string;

    @Column({ name: 'common_user_id', type: 'uuid', nullable: true })
    commonUserId?: string;
}
