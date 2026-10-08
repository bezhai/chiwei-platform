# AGENTS.md

本项目的 AI 协作规范分布在以下文件中，所有 AI Agent 必须遵守：

- [CLAUDE.md](./CLAUDE.md) — 项目结构、消息链路、泳道、部署命令、操作边界、开发与上线规则、赤尾设计原则
- [.claude/rules/e2e-testing.md](./.claude/rules/e2e-testing.md) — 飞书 dev 泳道端到端测试
- [.claude/rules/paas-engine.md](./.claude/rules/paas-engine.md) — PaaS Engine 开发指南（仅 `apps/paas-engine/` 下生效）
- [MANIFESTO.md](./MANIFESTO.md) — 赤尾宣言，禁止修改

## Codex 主会话的机制映射

CLAUDE.md 以 Claude Code 的机制描述，规则本身适用于所有 AI 工具。Codex 作为主会话时：

| Claude Code 机制 | Codex 等价 |
|---|---|
| 子 agent | `spawn_agent`（在 prompt 里说明任务性质：调研还是实现） |
| `/ship`、`/ops` 等 slash command | 按 CLAUDE.md 和对应 `.claude/skills/*/SKILL.md` 手动执行 |
| `.claude/hooks/enforce-routing.sh` | **不生效**，CLAUDE.md「操作边界」里的限制只能靠自律 |
| `.claude/settings.json` 权限 | **不生效**，同上 |
