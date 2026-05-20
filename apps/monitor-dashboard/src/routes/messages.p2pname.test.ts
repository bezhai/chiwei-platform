import { describe, it, expect } from 'bun:test';
import { P2P_NAME_SQL } from './messages';

// 身份全局化后 p2p 会话名取自 conversation_messages.username 冗余列。
// 子查询用 DISTINCT ON (chat_id) + ORDER BY chat_id, create_time DESC 取
// "最新一条 user 消息"的名字。但最新一行 username 可能为空（拉不到发送
// 者名时落 null，决策：不写脏占位），若不过滤 NULL，DISTINCT ON 会选中
// 这条空行，导致整段私聊丢掉本来更早可用的名字。本测试钉死：p2p 取名
// 子查询必须带 username IS NOT NULL 过滤。不连真实 DB —— 只断言 SQL 文本。

describe('messages.ts p2p 取名子查询', () => {
    it('带 cm.username IS NOT NULL 过滤，避免最新一行空名时丢更早可用名', () => {
        const sql = P2P_NAME_SQL.toLowerCase();
        expect(sql).toContain('cm.username is not null');
        // 仍是 user 行 + DISTINCT ON 取最新（不改原取名口径）
        expect(sql).toContain("cm.role = 'user'");
        expect(sql).toContain('distinct on (cm.chat_id)');
    });
});
