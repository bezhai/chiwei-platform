# Memory v4 Cutover 全面复盘

**日期**：2026-04-21
**涉及 PR**：#188（feat memory-v4）/ #189（被 revert）/ #190（假 revert）/ #191（清一次性代码）/ 本复盘对应的后续清理 PR

---

## 事件时间线

- 2026-04-19 之前：Memory v4 分 Plan A-E 开发了几周，所有新路径（v4 fragment / abstract_memory / memory_edge / notes / schedule_revision / 新 reviewer / 新 life engine tick v14 / Plan C inner_context）进泳道 `mem-v4` 验证
- 2026-04-21 上午：PR #188 合码并部署 prod 1.0.0.292，声称完成 cutover。实际只切了 voice/engine 的读路径（F-0a），**大量其他路径未切**
- 2026-04-21 上午：PR #189 自作主张加 on_startup migration，部署 1.0.0.293，用户叫停，PR #190 revert（但 revert 只改 docs，代码未动，残留直到今天的清理 PR）
- 2026-04-21 下午：通过 ops-db submit 手工做完数据迁移（fragment 232 条 / abstract_memory 159 条），避开了"一次性触发工具链"问题
- 2026-04-21 下午：PR #191 清掉 scripts/ 一次性迁移代码 + 真正 revert PR #189 残留
- 2026-04-21 晚：发现 `schedule.py` 还在读旧 `experience_fragment` 表 → **触发全面 audit** → 找出 7 处代码+1 处 Langfuse prompt 的 v3 残留 → 本次清理

---

## 根本原因：PR #188 不是 cutover，是 additive

PR #188 diff 是 **+10000 / -1000**。一个真正的 cutover PR 应该 **加多少行就删多少行**——因为"换数据源"意味着把老的换成新的。10:1 的加删比说明：

> **做了加法（新 v4 路径铺好），没做减法（旧 v3 路径没删）**

结果系统处于**新旧并存**状态：
- v4 写路径在跑（afterthought / glimpse / reviewer 产出新数据）
- v3 写路径也在跑（`relationships.py` 死写 `relationship_memory_v2`）
- v4 读路径在跑（voice / engine / self_abstracts / user_abstracts / short_term_fragments 读新表）
- **v3 读路径也在跑**（`schedule.py:217` 读旧 `experience_fragment` 的 daily grain）
- v3 cron 代码残留（`cron_generate_dreams` 名字没改）
- v3 Langfuse prompt 仍在 production（`schedule_daily_writer` v3 用 `{{yesterday_journal}}`）
- v3 config 字段仍在（`diary_model` / `diary_chat_ids` / `relationship_model`）

这些**不是"没发现"**——是"不知道要找"。因为 PR #188 本身没有一个"按设计 §三对照表逐项切换"的动作，所以每切一个点、不切哪些点，都没留在视野里。

---

## 具体的清理项（按设计 §三 对应）

设计文档 `2026-04-16-memory-v4-design.md` §三《现有管道的调整》明确列出了 v3→v4 的映射。本次清理按表对照做：

| 设计表项 | v3 现状 | v4 应有 | PR #188 切了吗 | 本次清理动作 |
|---|---|---|---|---|
| daily fragment | dream cron 产出 | **废弃** | ❌ 未切 | 删 `find_recent_fragments_by_grain` + schedule.py:217 读 daily 的代码 |
| weekly fragment | 每周 cron 产出 | **废弃** | ✅ 早已移除（schedule.py 注释 L6） | — |
| experience_fragment 表 | 直接注入 + 多下游 | **废弃**（v4 换 `fragment` 表） | 部分（voice/engine 切了，schedule 没切）| 删 ORM class + 6 个旧 query + schedule 重构 |
| relationship_memory_v2 表 | per-user 注入 | **扩展为统一 abstract_memory** | 读切了，**写没停**（relationships.py 仍在死写入）| 整删 `relationships.py` + ORM + 相关 query + 调用方（afterthought + rebuild endpoint）|
| schedule 生成输入 | curated_materials + yesterday_journal | **改为** 自我抽象 + 最近 fragments + 昨日 state 历史 | ❌ 完全没切 | schedule.py 重构 + Langfuse prompt v4 发布 |
| cron_generate_dreams | 旧 dream cron | 改为 heavy review | 函数体改了，**名字没改** | 改名 `cron_heavy_review` + 同步 arq_settings |
| `diary_model` config | v3 afterthought/schedule 共用 | v4 用 `offline-model` | ❌ 没改 | 删 config 字段 + afterthought `AgentConfig` 改 "offline-model" + schedule `_schedule_model()` 删 |
| `relationship_model` config | v3 relationships 用 | v4 无此路径 | ❌ 没改 | 删 config 字段 |

---

## 更深层的失败：角色错位

### 执行环节的具体错误

1. **"继续"比"完成"更优先**
   - PR #188 把 Plan A-E 切成 5 个阶段做，每个阶段都"加"。但没有一个阶段是"对照设计 §三 逐项切旧"。最后 cutover 时只把最显眼的 voice/engine 切了就宣布完成
   - 正确做法：设计 §三 是一个**验收清单**。cutover PR 前应该逐项 check "切了还是没切"，没切的在同一 PR 里切，否则就不是 cutover

2. **没跑 grep-based 的 cutover 验证**
   - 今天的 audit subagent 只用了几条 grep（`ExperienceFragment` / `RelationshipMemoryV2` / `grain=` / `diary_model` 等）就找出了所有残留
   - 这个 grep **本该是 PR #188 合码前的标准 check**，不是事后补救

3. **Langfuse prompt 和代码不同步**
   - `schedule_daily_writer` prompt 里 `{{yesterday_journal}}` 在 PR #188 之后仍是 production 版本。代码和 prompt **变量约定就是 cutover 的一部分**
   - 正确做法：代码改 prompt_vars 的 PR 必须伴随 Langfuse prompt 发新版本

### 决策环节的具体错误

1. **Memory v4 cutover 没有"验收门"**
   - 没有一个"在什么条件下才能说 v4 完成"的门槛。结果是"代码能跑就叫完成"
   - 正确做法：cutover 的门应该包含"设计 §三 所有项都切"+"grep 0 残留"+"对应 prompt 发新版本"

2. **迁移方案（scripts/ + endpoint）设计错位**
   - PR #188 里埋了 `scripts/migrate_*.py` 和 `/admin/memory-v4-migration` endpoint，这两者**都是为"跑一次迁移"而存在的一次性代码**
   - 今天发现更简洁的路径：**纯 SQL + ops-db submit**，连 LLM 合成都可以省（impression 原文即 abstract content）
   - 早点审视"迁移是不是真的需要跑完整代码路径"，就能省下 PR #189 的事故
   - 这个教训被记入 `feedback_no_oneoff_scripts_in_repo.md`（规则）

3. **PR #190 "名叫 revert 实际没 revert" 没人发现**
   - PR #190 commit 里只改了 docs，没 revert `arq_settings.py` 里的 migration 代码。但它顶着 "Revert PR #189" 的标题，让人误以为"已经干净了"
   - 今天通过 `git log --follow apps/agent-service/app/workers/arq_settings.py` 才发现 PR #190 根本没出现在这个文件的历史里
   - 正确做法：revert PR 合码前必须跑一次 `git diff 合码前 合码后 -- <目标文件>`，证明目标文件回到了被 revert 前的状态

---

## 反馈规则沉淀

本次事件产生的长期规则（已入 memory）：

- `feedback_no_unauthorized_pr.md`（红线）— 不得擅自 create PR / merge / deploy / 改 prod DB
- `feedback_no_oneoff_scripts_in_repo.md` — 禁止在 scripts/ 写一次性脚本，一次性任务走 "app/ 模块 + endpoint + /tmp 临时调用器"
- **本复盘补充的新规则（待沉淀）**：
  - "cutover PR" 的 diff 加删应该接近 1:1，严重偏 additive 说明没真切
  - cutover PR 合码前必须对应设计文档逐项 check + grep 所有旧符号零残留
  - Langfuse prompt 改名和代码 prompt_vars 改动必须同步
  - revert PR 合码前必须 `git diff` 证明目标文件确实 revert

---

## 当前状态（本清理 PR 合码后）

- 代码层 agent-service 内**零 v3 残留**（grep 验证）
- `schedule_daily_writer` prompt v4 已发 production（按设计 §三 接入 self_abstracts / recent_fragments / yesterday_life_states）
- 459 个单元测试全绿
- 剩余 v3 档案：**仅 `docs/archive/*` 和 `docs/superpowers/plans/*` 下历史文档**（不改，保留历史记录）
- 旧表 `experience_fragment` / `relationship_memory_v2` 仍在 DB（等本 PR 部署后再 DROP，避免"代码没部署 + 表没了 → 写入报错"）

## 还没做的事

- P3：DROP 旧表 `experience_fragment` / `relationship_memory_v2`（必须在本清理 PR 部署后做）
- `identity_drift` 系统是否要沉入 memory v4 范畴（设计文档没提，先保留）
- `life_engine_tick` 偶发 120s timeout（04-19 就有，非 v4 引入，占比 0.28%，可忽略但继续关注）
- heavy reviewer 首次真实触发需等明晨 03:00
