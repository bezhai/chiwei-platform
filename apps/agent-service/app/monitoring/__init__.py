"""带外监控探针 —— 独立于 agent 心跳的基建存活检查。

这里的探针**只观测、只告警、绝不替 agent 决策**：不叫醒任何角色、不跑世界推演、
不启动任何 runtime source loop。它们寄生在独立于 agent-service world 心跳的基建上
（独立 CronJob / 周期检查），兜住「world 自己的进程或心跳源挂了」这唯一没被业务
逻辑覆盖的单点。
"""
