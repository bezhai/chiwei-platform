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
