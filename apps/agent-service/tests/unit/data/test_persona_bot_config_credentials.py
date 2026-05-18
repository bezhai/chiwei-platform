"""bot_config 多 channel 化后，agent-service 侧读 bot 凭据的调用方必须跟着改。

唯一读 bot_config 凭据列的地方是 ``resolve_mentioned_personas``：原来
``WHERE app_id = ANY(:mentions)`` 直接吃裸 ``app_id`` 列。channel-server 把飞书
凭据迁进 ``credentials`` JSONB 并删了独立列后，这条 SQL 必须改成查
``credentials->>'app_id'``，否则 mention -> persona 路由会全军覆没（查不到任何
bot，@ 赤尾在群里永远不回）。

这里把那条 SQL 抽成模块级常量并断言它走 JSONB 路径、不再碰裸列——这是对 DB
schema 契约的真实断言，不依赖活库。
"""
from app.data.queries.persona import MENTIONED_PERSONAS_SQL


def test_mentioned_personas_sql_reads_credentials_jsonb_not_bare_column():
    sql = MENTIONED_PERSONAS_SQL.lower()
    # 必须经由 credentials JSONB 取 app_id（pg ->> 文本提取）
    assert "credentials->>'app_id'" in sql.replace(" ", "")
    # 不允许再出现裸 app_id 列条件（旧列已删，留着会报 column does not exist）
    assert "where app_id" not in sql
    assert "app_id = any" not in sql
    # 仍按 mentions 数组过滤、只取启用且有 persona 的 bot（行为不变）
    assert ":mentions" in sql
    assert "is_active = true" in sql
    assert "persona_id is not null" in sql


def test_mentioned_personas_sql_is_channel_scoped_to_lark():
    """mention -> persona 路由必须限定 channel='lark'。

    QQ 的 credentials 也有 app_id，跨 channel 命名空间会撞：飞书 mention 传进
    一个恰好等于某 QQ bot app_id 的值时，没有 channel 约束就会误命中 QQ persona
    （误路由）。恢复与旧飞书裸 app_id 列等价的语义 = 只在飞书 bot 里查。
    """
    sql = MENTIONED_PERSONAS_SQL.lower()
    assert "channel = 'lark'" in sql
