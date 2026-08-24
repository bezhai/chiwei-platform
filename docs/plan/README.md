# 设计文档索引

本目录存放 spec、plan、handoff 等设计文档。按主题分组，新文档请在对应分组追加。

## 赤尾自主化重设计（world / life engine）

| 文件 | 内容 |
|---|---|
| `chiwei-redesign-vision.md` | 重做第一步：现状卡点与目标 |
| `chiwei-redesign-world-model.md` | 重做第二步：世界模型构成 |
| `chiwei-redesign-day-walkthrough.md` | 赤尾世界 · 模拟一天 |
| `world-life-engine-brainstorm.md` | world / life engine 脑暴 |
| `chiwei-life-engine-rewrite-facts.md` | 重写 life / world engine 的现状实证笔记 |
| `chiwei-world-life-autonomy-redesign.md` | world / life 自主化重设计目标架构 |
| `chiwei-world-life-rewrite-spec.md` | world engine 推动的三姐妹 event 世界 |
| `chiwei-world-life-agent-loop-spec.md` | 从 structured-output 填表改为 agent 工具循环 |
| `chiwei-world-life-stateful-memory-spec.md` | 会话续接（session_id + Redis 上下文） |
| `chiwei-world-objective-event-driven.md` | world 纯客观事件驱动 + life 事件反应 |
| `chiwei-world-objective-progression.md` | world 客观大纲自维护 + world/life 职责重划 |
| `chiwei-world-self-schedule-cst-time-spec.md` | world sleep 定节奏 + 时间出口统一 CST |
| `chiwei-stage0-foundation-spec.md` | 阶段 0 地基实现 |
| `chiwei-stage1a-autonomy-spec.md` | 阶段 1A：world 退成推演者 + 角色自主 |
| `chiwei-stage1b-self-schedule-spec.md` | 阶段 1B：world / life 自排唤醒 |
| `chiwei-living-world-design.md` | 活的世界：身份随时间流动 |
| `chat-life-wake-flow-sketch.md` | Chat → Life 感知链路草图 |

## 赤尾能力与行为

| 文件 | 内容 |
|---|---|
| `chiwei-more-human-directions.md` | 更像人的下一阶段迭代方向 |
| `chiwei-group-proactive-messaging-spec.md` | 群里主动说话：群成为一等可投递对象 |
| `chiwei-life-proactive-messaging-spec.md` | life 自主发消息：收拢旧旁路 |
| `chiwei-proactive-chat-turn-render.md` | 发给真人的消息统一走 chat-turn 人设渲染 |
| `chiwei-life-web-access-spec.md` | life 上网能力 |
| `chiwei-memo-schedule-spec.md` | 备忘录 & 日程 |
| `chiwei-notebook-todo-memo-split-spec.md` | notebook 拆分待办与随笔 |
| `chiwei-novel-reading.md` | 赤尾读小说 |
| `chat-context-normalization-spec.md` | 对话 context 规范化 |
| `chat-context-normalization-handoff.md` | 对话 context 规范化 · 工作交接 |
| `chiwei-life-chat-context-realtime-handoff.md` | chat→life 对话感知重构 · 开发交接 |

## 多渠道与渠道层

| 文件 | 内容 |
|---|---|
| `multi-channel-support.md` | 多渠道接入改造总纲（QQ 作为第一个验证 channel） |
| `multi-channel-PR228-review.md` | PR #228 多渠道改造全局 Review |
| `multi-channel-T5c-readside-review.md` | 多渠道身份全局化 · 读取侧变更（T5-5c） |
| `common-channel-layer-design.md` | common/channel 分层身份设计 |
| `channel-layer-redesign.md` | channel 层目标架构 |
| `channel-layer-redesign-detail.md` | channel 层数据模型 |
| `channel-layer-redesign-tech.md` | channel 层技术设计 |
| `channel-proxy-redesign.md` | channel-proxy 退化设计（C3） |
| `identity-migration-runbook.md` | 身份层迁移 Runbook（C2，**已废弃**） |
| `qq-channel-integration.md` | QQ 渠道接入 |
| `lark-service-split-spec.md` | 飞书渠道拆分为独立服务 lark-service |

## 泳道与路由

| 文件 | 内容 |
|---|---|
| `lane-routing-redesign.md` | 泳道分流重新设计 |
| `lane-propagation-fix-spec.md` | 修复 lane 跨服务传递 |

## Agent 基建与框架

| 文件 | 内容 |
|---|---|
| `agent-infra-design.md` | Agent 基建设计：dataflow 框架即 agent fabric |
| `agent-runtime-self-build-research.md` | 自研 Agent Runtime 调研（去 langchain / langgraph） |
| `agent-service-refactor.md` | agent-service Framework 重构 Plan |
| `skill-assets-ci-gate.md` | Skill Assets CI Gate 维护 |

## 运维与工具

| 文件 | 内容 |
|---|---|
| `ops-db-chiwei-test.md` | ops-db 支持 chiwei-test 库 |
| `media-sync-internal-deps.md` | pixiv 鉴权字段注入迁内网 |
