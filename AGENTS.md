# AGENTS.md

本项目的 AI 协作规范分布在以下文件中，所有 AI Agent 必须遵守：

## 主文档

- [CLAUDE.md](./CLAUDE.md) — 项目结构、核心数据流、部署命令、AI 行为约束

## 规则文件

- [.claude/rules/safety-rules.md](./.claude/rules/safety-rules.md) — 安全与工具链规范（改动审批、五条禁令、环境变量管理）
- [.claude/rules/merge-and-ship.md](./.claude/rules/merge-and-ship.md) — 合码与 Ship 铁律（必须等用户确认、列出所有改动、冲突展示）
- [.claude/rules/e2e-testing.md](./.claude/rules/e2e-testing.md) — 飞书 Dev 泳道端到端测试流程
- [.claude/rules/paas-engine.md](./.claude/rules/paas-engine.md) — PaaS Engine 开发指南（仅 `apps/paas-engine/` 下生效）

## 宪法

- [MANIFESTO.md](./MANIFESTO.md) — 赤尾宣言，禁止修改

## Codex 主会话流程映射

CLAUDE.md 的开发流程以 Claude Code 机制描述，流程本身适用于所有 AI 工具。Codex 作为主会话时，机制映射如下：

| Claude Code 机制 | Codex 等价 |
|---|---|
| Explore / general-purpose 子 agent | `spawn_agent`（在 prompt 里说明任务性质：探索或实现） |
| `/spec`、`/ship` 等 slash command | 按 CLAUDE.md 流程手动执行；skill 逻辑仍可通过 skill 系统触发 |
| plan 模式（safety-rules 的改动审批） | 先出方案等用户确认，不直接改 |
| `.claude/hooks/enforce-routing.sh` | **不生效**。安全规则靠文档自律，无自动化拦截 |
| `.claude/settings.json` 权限 | **不生效**。同上 |

T1/T3/T4 review 检查点不变，通过 `agent-collaboration` skill 调用外部 Agent。当前已登记的 adapter 只有 `codex-worker`（Codex CLI）；没有 Claude adapter，Codex 主会话时 review 仍走 codex-worker（可换模型获得一定独立性）。
