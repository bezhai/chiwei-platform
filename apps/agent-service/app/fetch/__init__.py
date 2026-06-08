"""通用抓取 agent —— 每天清晨把外部底料整理成简报落库，给 world 当背景知识（刀 3 Task2）。

三块：
  * :mod:`app.fetch.materials` —— ``DailyMaterials`` durable Data + 落库/读回契约。
  * :mod:`app.fetch.agent`     —— 抓取 agent 的 AgentConfig + system prompt id。
  * :mod:`app.fetch.node`      —— cron → 单字段 tick → 翻译补 lane → 抓取节点的链路。

cron wiring 在 ``app.wiring.fetch_dataflow``。
"""
