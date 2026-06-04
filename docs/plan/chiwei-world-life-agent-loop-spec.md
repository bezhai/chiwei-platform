# 赤尾 world/life engine：从 structured-output 填表改为 agent 工具循环

## Problem

world 和 life 现在都用 `Agent.extract(...)` 一次性提取一个结构化大对象（world 提
presence + events 两个列表，life 提状态字段）。这本质是让一个 agent 特化的模型「填
一张表」。coe 实测下来 world 在平淡时段（工作日下午）判断「没够格成 event 的变化」
就交空表，世界凝固、life 没东西可反应。根子不在 prompt 措辞克制，而在范式：把一个
被训练成「连续调用大量工具去行动」的模型，憋成「填一次表就结束」，既压抑了它持续
行动的倾向，也没用上它的 agent 能力。

## Goal

world 和 life 都改成跑一个 **agent 工具调用循环**：被唤醒后在循环里**连续调用工具
去行动**（world 移动谁、在哪产生什么动静、待会睡多久；life 更新自己此刻的状态、要
不要起意图），直到自己不再调工具就收口。「多产 event」不再靠 prompt 求，而是这个
范式的自然结果。

emit 全程**异步 fire-and-forget**：调 emit 只是把 event 丢进信箱队列（落库 + 敲门），
立即返回，绝不在循环里等 life；life 照旧在 debounce consumer 里异步醒来。和现有
`deliver_event` 行为天然一致。

可观测：每个角色（world 与三姐妹各自）的所有思考归到**她自己一条、按天滚动的
langfuse session**，能连续看一个角色的「意识流」。

## Non-goals

- **不碰 agent 框架 runtime**。现有多轮工具循环、`@tool` 反射定义、dispatch 都已在
  chat 链路产线验证，直接复用。
- **不动唤醒可靠性那层**。deliver / 敲门 / debounce / 信箱对账自愈（本分支已 ship）
  保持不变。
- **不做 direct / trace 两类 event**。仍是第一刀范围：环境感知（ambient）+ 意图。
- **context 按需取 + 压缩，后续做**。第一刀只喂精简的最近状态；像 agentic coding 那样
  「用工具按需查历史 + 长了压缩」是紧接着的下一件大事，不塞进第一刀。
- **world 睡长觉，后续做**。第一刀钉死「最长睡 1h」的保底，「深夜睡 6 小时」这类要
  松上限 + 配别的踹醒机制，留后。
- **跨进程因果串联 session，后续做**。第一刀只做「每角色按天一条」的分组，不追求把
  「world→event→life→intent→world」串成一条 trace（debounce 折叠让它不可靠）。

## Key design decisions

1. **world 工具集 = 移动某人 / 产生客观动静 / 决定下次多久再看**，而不是返回对象。
   产生动静的工具锚定房间、只投给当时在场者（产生侧在场过滤、信息差命门不变），且
   异步（落库 + 敲门即返回）。「下次多久再看」是 world 唯一的自排手段（详见决策 4 的
   sleep 语义）。当前「谁在哪 / 现在几点 / 刚才大致什么样」作为 prompt 一次喂全，不另
   设查询工具——第一刀从简。

2. **life 工具集 = 更新自身主观状态 / 起意图**。她信箱里本轮未读 event 作为 context；
   循环收口后由代码统一标已读（只标本轮读到的那批，沿用现有正确性命门）。语义补全：
   更新状态可调 0 次或多次，**多次以最后一次为准**，一次都不调也照常标已读（她看了
   但没改状态，正常）。空信箱 early-return、`single_flight` 锁两条命门保留。life 的
   context **绝不含 world 全局快照**（信息差命门）。

3. **工具副作用的幂等与失败语义**（chat 工具是只读 / 幂等，world/life 工具是 durable
   写，失败模型必须不同）：① 循环中途模型调用瞬时失败时**绝不整轮重放已执行的
   durable 副作用**——断了就收口本轮已做的，靠 world 心跳 + 信箱对账下次补；② 工具
   尽量幂等（event 的去重键由内容 / 序号派生，不每次新生成）；③ 单个工具自身抛错时
   吞掉、把错误喂回模型让它自纠，不炸整轮。

4. **收口与安全阀**（不把「模型自然停」当唯一控制面）：① 一轮的工具调用轮数与 emit
   条数设安全阀，正常够不着、一旦触及要 log 不能静默截断，并明确触顶时的收口动作；
   ② life 一轮读信箱设上限，积压过多时分批；③ **sleep 上限 1h，超过即报错、把错误喂
   回模型重调**（不静默夹）；没调 sleep 就收口 → 落到保底心跳（最长 1h 必醒一次）。
   保底心跳是「world 是世界发动机、睡死了没人能踹醒」的安全网；睡多久她自己定、代码
   只兜「别睡死」的底——这是机制边界（「何时停 / 何时醒」），不是用阈值替「产什么」
   的决策，不违赤尾宪法。安全阀全部要有观测指标。

5. **session 按角色、按天滚动**：每个角色用确定性的 session 编号（按 lane + 角色 + 日期
   生成，不靠 event 跨进程传递，故绕开 debounce 折叠问题），她当天所有唤醒的 LLM 调用
   都归进去。需在 agent core 补 session 入口（按 langfuse 真实语义设到 trace 上、而非
   trace_context）。

6. **world 只落 world_time，不让模型写 situation**。让模型写 situation 会退化成隐藏的
   填表 + 第二事实源；下次唤醒的「刚才大致什么样」由「谁在哪 + 最近 event」重建（若需
   一句概况，仅由代码从本轮动作派生作观测用，不让模型产）。

7. **clean-slate、无兼容层**：删掉填表用的 BaseModel 与 extract 路径，节点内部重写成
   agent 循环；wiring（三源 → world、event.debounce → life、intent → world）不变，变的
   只是节点内部如何「思考」。

## Caller coverage（节点内部重写，外部 wiring 与调用契约不变）

- world 节点（被三源唤醒）：内部从 extract + 手动消费，改为 agent 循环 + 工具 handler。
- life 节点（被 event.debounce 唤醒）：内部从 extract + 手动消费，改为 agent 循环 + 工具
  handler；并发锁 / 空信箱 early-return / 标已读收口保留。
- agent core：新增 session 入口，向后兼容（不传同现状），chat 链路不受影响。
- langfuse 的 world / life 两个 prompt（coe 泳道 label）：从「填表指令」改写为「你是
  world / 你是赤尾，用这些工具行动」的 agent 指令，变量契约随之核对。

## Data & deployment impact

- **表**：world 快照只留 world_time（决策 6）；其余 data_* 表不变。删 BaseModel 不影响已
  建表，无新表、无 schema 迁移。
- **langfuse**：world / life 两个 prompt 在 coe 泳道 label 改写（泳道隔离、不碰 production），
  改后用 trace 验证渲染。
- **部署**：改 agent-service 核心，需重新部署 coe（agent-service + 同步 vectorize-worker），
  清空 6 张 data_* 表从干净状态冷启动重跑；部署会重启 pod、中断当前 world 心跳——coe
  测试泳道，无妨。验证期可临时把 sleep 上限 / 保底心跳调短以快看效果，上线调回 1h。
- **观测**：验证要能在 langfuse 按「每角色按天一条 session」看到一个角色一天的连续意识流。

## Tasks

world 与 life 一起实现（同构、一份设计），下列任务按依赖推进。

1. **agent core 补 session 入口**
   - 目标：让每个角色当天的所有 LLM 调用归到她自己一条 session。
   - 产出：core 支持按确定性编号把一次调用归入指定 session；按「lane + 角色 + 日期」生成
     编号的约定。
   - 验收：coe 跑一轮后，langfuse 里同一角色当天的多次唤醒落在同一条 session 下，不同
     角色 / 不同天分开。

2. **world 转 agent 工具循环**
   - 目标：world 被唤醒后在循环里连续行动（移动 / 产生动静 / 定下次多久再看），平淡时段
     也持续产 event；落实工具的幂等 / 失败语义与安全阀（含 sleep 上限 1h 超限报错）。
   - 产出：world 工具集 + 重写后的 world 节点 + 改写的 world langfuse prompt（agent 工具
     指令、客观投影不碰情绪、鼓励主动推进、不写 situation）。
   - 验收：coe world 唤醒后，trace 显示她在一个循环里连续调工具、单轮产出多条 event；
     平淡时段 event 信箱也持续增长；在场随移动更新；sleep 行为符合「自定 ≤1h、超限报错、
     没调落保底」。

3. **life 转 agent 工具循环**
   - 目标：life 被 event 唤醒后在循环里更新自身状态、按需起意图，输出来自工具调用而非
     填表；保留并发锁、空信箱 early-return、收口标已读、信息差。
   - 产出：life 工具集 + 重写后的 life 节点 + 改写的 life langfuse prompt。
   - 验收：coe life 被唤醒后 trace 显示在循环里调工具；状态被更新、必要时起意图回灌
     唤醒 world；context 不含 world 全局快照（信息差不破）；0/N 次更新与标已读语义符合
     决策 2。

4. **删旧填表范式 + 切测试 + 真机验证**
   - 目标：旧 extract 范式零残留，测试覆盖新范式的真实行为（工具被调 + 副作用 + 失败 /
     安全阀边界），coe 端到端跑通。
   - 产出：删除填表 BaseModel 与 extract 路径；测试从「mock extract 返回对象」改为「验证
     工具调用与 handler 副作用、并覆盖幂等 / 失败 / 安全阀」；旧类名 / 旧路径 grep 零残留。
   - 验收：全量测试绿；旧范式 grep 零命中；coe 清空重跑后，每角色按天 session 串起她当天
     的连续运转，世界不再凝固、life 有状态产出。
