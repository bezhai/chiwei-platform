"""chat — 只剩一个解析工具。

会话 pipeline（router / context / render / stream / post-actions）随旧实现整体
删除。留下的是 :mod:`app.chat.content_parser`：把公共层 ``content`` 列那串
``{kind, key}`` 项解析成文本和附件，跟哪套引擎在跑无关，``app.data.queries.messages``
仍在用。

这里**不 re-export 任何东西**：``import app.chat.content_parser`` 会先执行本文件，
在这儿 import 一个已经删掉的模块会让那次 import 连带炸掉。
"""

__all__: list[str] = []
