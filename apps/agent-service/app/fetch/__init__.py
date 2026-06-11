"""每日底料的钟与落脚处 —— 「fetch」概念已消解（眼睛 Task 3）。

认知层（看什么、怎么看、怎么叙述）在 :mod:`app.world.eyes`（world 的感官器官），
这个包只剩钟与落脚处两块：

  * :mod:`app.fetch.materials` —— ``DailyMaterials`` durable Data + 落库/读回契约。
  * :mod:`app.fetch.node`      —— cron → 单字段 tick → 翻译补 lane → 眼睛节点的链路
    （早退检查 / 落库 / 记成本）。

cron wiring 在 ``app.wiring.fetch_dataflow``（白天每小时打点，失败下一钟点重试）。
包名不改——dataflow 信号 kind（DailyMaterialsTick / DailyMaterialsFetch）改名会让
MQ 遗留旧 schema 消息反序列化失败（踩过的 coe cutover 坑）。
"""
