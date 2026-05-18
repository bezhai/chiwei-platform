"""bot_config 多 channel 化后 tool-service 侧读 bot 凭据的调用方必须跟着改。

tool-service 的 init_lark_clients 原来直接读 bot.app_id / bot.app_secret 裸列建
Lark SDK client。channel-server 把飞书凭据迁进 credentials JSONB 并删了 app_id /
app_secret 独立列后，再 SELECT 旧列运行期会 column does not exist。

这里把 "从一条 bot_config 记录解释飞书 app_id/app_secret" 抽成纯函数断言：
非 lark 的记录被跳过、缺凭据明确抛错、lark 记录从 credentials JSONB 取出与旧裸
列等价的 app_id/app_secret。不依赖活库。
"""
import pytest

from app.infrastructure.lark_client import lark_credentials_from_row


class _Row:
    def __init__(self, bot_name, channel, credentials):
        self.bot_name = bot_name
        self.channel = channel
        self.credentials = credentials


def test_lark_row_credentials_from_jsonb():
    row = _Row(
        "chiwei",
        "lark",
        {
            "app_id": "cli_xxx",
            "app_secret": "sec_xxx",
            "encrypt_key": "ek",
            "verification_token": "vt",
            "robot_union_id": "ru",
        },
    )
    creds = lark_credentials_from_row(row)
    assert creds == ("cli_xxx", "sec_xxx")


def test_non_lark_row_is_skipped():
    row = _Row("qqbot", "qq", {"app_id": "qq_app", "app_secret": "qq_sec"})
    assert lark_credentials_from_row(row) is None


def test_lark_row_missing_credentials_raises():
    with pytest.raises(ValueError, match="credential"):
        lark_credentials_from_row(_Row("chiwei", "lark", None))


def test_lark_row_missing_field_raises():
    with pytest.raises(ValueError, match="app_secret"):
        lark_credentials_from_row(
            _Row("chiwei", "lark", {"app_id": "cli_xxx"})
        )
