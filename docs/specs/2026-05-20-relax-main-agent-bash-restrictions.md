# Spec: 放松主 agent Bash 元字符限制

## 背景

当前 `.claude/hooks/enforce-routing.sh` 在主会话上跑 Bash 时，命令里只要在引号外出现 `$(` / 反引号 / `>` / `<`（含 `>>` `<<` `>(` `<(`）就一刀切拦截，视为 allowlist argv0 夹带未审计 repo touch 的 Trojan。这条规则把日志查看的高频组合也砸到了：`make logs APP=xxx LANE=xxx SINCE=10m 2>&1 | tail -150`——`2>&1` 里的那个 `>` 是 fd 描述符合并，根本不触发文件 I/O，但规则不区分语义，命中即拦，导致主会话连看日志都要派子 agent。

铁律"主会话对仓库文件零直接接触"的本质动机是上下文隔离（feedback_file_ops_must_use_subagent），适用于 Read/Edit/Write/Grep/Glob 这类文件工具和真正会读写仓库内容的 Bash。fd 描述符重定向不属于这类——它纯粹是输出整形，没有任何 repo touch 的可能性。规则应该按"是否触发对仓库内容的实际读写"来分，而不是"是否含某个字符"。

## 目标

让主会话能直接跑常见运维查询 + 看日志命令（含 fd 合并、管道整形、命令串联），同时保持"主会话对仓库文件零直接接触"的铁律不变；走私规则（`$( ` 反引号、文件重定向、输入重定向）继续硬拦。

## 不做什么

- 不动文件工具铁律：Read / Edit / Write / Grep / Glob / MultiEdit / NotebookEdit / LSP 主会话仍全拦。
- 不动安全层：`kubectl` 写、`curl` 写、直连 `.svc.cluster.local` / `localhost:*` / 内网 IP、`psql` `mysql` `mongosh` `redis-cli` 等数据库客户端，主子均拦。
- 不放行 `>` `>>` 文件重定向（哪怕目标在 `/tmp/`）——一旦引入路径白名单就要解析引号、变量展开、相对路径，复杂度和误判风险陡增，收益小到可以用 `| tee /tmp/x` 或 `| tail -N` 全替代。
- 不放行 `$( ... )`、反引号、`<`、`<<<`、`<( ... )`——这几种是 Trojan 风险源（命令替换可能把 repo 内容当参数 leak，输入重定向可能从 repo 读文件），主会话继续禁用。
- 不动子 agent 行为：子 agent 除安全层外全部放行的现状保持。
- 不引入 spec 流程 / TDD discipline / 任何工作流层面的改动，本 spec 只动一条 hook 规则 + 一处 CLAUDE.md 表述。

## 关键设计决策

判定标准从"是否含某个元字符"重写成"**是否触发对仓库内容的实际读写**"。按这个标准重新分类：

`2>&1`、`&>`、`>&2`、`>&N`、`<&N`、`&>>`（这里的 N 是数字 fd）这类纯 fd 描述符重定向不写任何文件，只是把一个 fd 复制 / 重定向到另一个 fd，理论上不可能 leak repo 内容也不可能写 repo 文件，应该放行。判定上有一个简单的语法特征：`>` `<` 后面紧跟 `&数字` 或前面是 `数字>&`，整体没有出现文件路径 token。

`>` `>>` 后跟一个路径 token（无论是 `/tmp/x`、`./foo`、`$HOME/y` 还是带引号的形式）继续拦——保守起见全拦，不引入路径白名单。如果用户真需要把输出写到 `/tmp`，可以 `| tee /tmp/x` 或派子 agent。

`$( ... )`、反引号、`<`、`<<<`、`<(` 继续拦，理由同"不做什么"。

管道（`|`）和串联（`;` `&&` `||`）按当前 hook 实现其实已经放行——每段独立走 argv0 allowlist 校验。本 spec 不修改这部分，只补测试覆盖确认行为。

CLAUDE.md "编排豁免边界"段（行 134 附近）的措辞需要同步更新一句，澄清"fd 重定向 + 管道整形 + 串联可主会话直接做，文件重定向 / 命令替换 / 输入重定向必须子 agent"，避免读者只看 CLAUDE.md 时误以为含元字符就一定要派子 agent。

## 调用方全覆盖

Hook 的唯一调用方是 Claude Code harness 的 PreToolUse 事件钩子，每次主会话或子 agent 发起 Bash / Read / Edit / Write 等工具调用前都会执行一次。直接消费者只有主会话和子 agent 两类。测试调用方只有 `.claude/hooks/test-enforce-routing.sh` 一个。CLAUDE.md "编排豁免边界"段是文档层面的语义来源，需要同步。

不存在间接消费者（hook 不被任何 CI、任何业务代码、任何 skill 脚本调用）。改动只影响主会话发起的 Bash 命令在元字符判定上的行为，子 agent 路径完全不动，安全层完全不动，文件工具铁律完全不动。

## 数据 & 部署影响

零 DB / Redis / MQ / 服务部署改动。影响范围只在本仓库三个文件：`.claude/hooks/enforce-routing.sh`、`.claude/hooks/test-enforce-routing.sh`、`CLAUDE.md`。hook 是 PreToolUse 每次执行 bash 脚本，改完下次主会话调用 Bash 时立即生效，不需要重启 Claude Code、不需要重启会话、不需要部署任何服务。

## 粗颗粒 Task

**T1 — 放行 fd 描述符重定向**
目标：让 `2>&1`、`&>`、`>&2`、`>&N`、`<&N` 等纯 fd 形式不再触发走私拦截，同时 `> /path`、`< /path`、`$(...)`、反引号继续拦。
产出：`.claude/hooks/enforce-routing.sh` 走私检测段（约行 249-342）的判定逻辑改动。
验收：原痛点命令 `make logs APP=channel-proxy LANE=coe-t5-id SINCE=10m 2>&1 | tail -150` 在主会话路径下不被 hook 拦；`git status > CLAUDE.md`、`git status >> ./foo`、`git status $(cat CLAUDE.md)`、` cat \`whoami\` `、`grep foo < src/bar.py` 在主会话路径下仍被拦。

**T2 — 补单元测试**
目标：把"哪些放 / 哪些拦"的边界用测试钉死，防止后续回归。
产出：`.claude/hooks/test-enforce-routing.sh` 新增 case，至少覆盖：fd 合并 `2>&1` 放行、`&>` 放行、`>&2` 放行、双引号内 `2>&1` 放行；`> /tmp/x` 仍拦、`>> ./foo` 仍拦、`$(...)` 仍拦、反引号仍拦、`< file` 仍拦、`<(...)` 仍拦；以及完整真实痛点 `make logs ... 2>&1 | tail -150` 放行。
验收：跑 `bash .claude/hooks/test-enforce-routing.sh` 全部 pass，case 数量较改前增加且新 case 与上述场景一一对应。

**T3 — 同步 CLAUDE.md 表述**
目标：澄清新边界，避免文档和 hook 行为脱节。
产出：CLAUDE.md "编排豁免边界"段（行 134 附近）补一句话，说明 fd 重定向 / 管道整形 / 串联在主会话可直接做，文件重定向 / 命令替换 / 输入重定向必须子 agent。
验收：用户人工 review 这一句表述清晰、与 hook 行为一致。
