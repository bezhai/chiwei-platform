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
- 不放行 `&>` 和 `&>>`——这两个不是 fd 操作，是 `cmd > out 2>&1` 的语法糖，右侧跟文件路径，会真的写文件。
- 不放行 `>&file` 这类右侧是文件名 token（非纯数字）的形式，等价于普通文件重定向。
- 不放行 `>(cmd)`、`<(cmd)` 进程替换——bash 会先把内部命令跑起来，等于给了任意命令构造通道。
- 不放行 `$(...)`、反引号、`<`、`<<<`——这几种是 Trojan 风险源（命令替换可能把 repo 内容当参数 leak，输入重定向可能从 repo 读文件），主会话继续禁用。
- 不放行 `tee`（写文件）、`less` / `more`（交互式终端工具）、`xargs`（任意命令构造），这些不进 stdout filter allowlist。
- 不对 stdout filter 的 argv 做"是否含 repo 路径"的精确判定。`tail CLAUDE.md`、`grep -r foo .` 不会被 hook 拦，这是 A 方案刻意承担的 surface，理由见关键设计决策段。
- 不动子 agent 行为：子 agent 除安全层外全部放行的现状保持。
- 不引入 spec 流程 / TDD discipline / 任何工作流层面的改动，本 spec 只动一条 hook 规则 + 一处 CLAUDE.md 表述。

## 关键设计决策

判定标准从"是否含某个元字符"重写成"**是否触发对仓库内容的实际读写**"。按这个标准重新分类。

放行的形式只有一种：**fd-to-fd 复制**，语法是 `[n]>&[m]` 或 `[n]<&[m]`，其中 m 必须是数字（也就是另一个 fd 编号），左侧 n 可以省略也可以是数字。典型例子有 `2>&1`、`1>&2`、`2>&3`、`3<&0` 等。这种形式只是把内核里的 fd 表项复制一份，不打开任何文件，不可能 leak repo 内容也不可能写 repo 文件。

必须明确剔除几类容易被错误归到"fd 重定向"里的形式：

`&>` 和 `&>>` 看起来像 fd 操作，实际是 `cmd > out 2>&1` 的语法糖，右侧跟的是文件路径，会真的写文件，**不放行**。

`>&file` `>&out` 这种右侧是文件名 token（不是纯数字）的形式，是 bash 的另一类语法糖，等价于普通文件重定向，**不放行**。判定标记是 `>&` 后面跟的不是纯数字 token。

`>(cmd)` 和 `<(cmd)` 是进程替换，bash 会展开成 `/dev/fd/N` 并先把内部命令跑起来——内部命令可以是任意东西，等于给了一条 Trojan 通道，**不放行**。

`>` `>>` 后跟任何路径 token（`/tmp/x`、`./foo`、`$HOME/y`、带引号的形式都算）继续拦，保守起见全拦，不引入路径白名单。如果用户真需要把输出写到 `/tmp`，可以 `| tee /tmp/x` 或派子 agent。

`$(...)`、反引号、`<`、`<<<` 继续拦，理由同"不做什么"。

管道（`|`）和串联（`;` `&&` `||`）按当前 hook 实现其实已经放行——每段独立走 argv0 allowlist 校验。本 spec 不修改这部分判定结构，只补测试覆盖确认行为。

CLAUDE.md "编排豁免边界"段（行 134 附近）的措辞需要同步更新，澄清"fd-to-fd 复制 + stdout filter argv0 可主会话直接做，文件重定向 / 命令替换 / 输入重定向 / 进程替换必须子 agent"，避免读者只看 CLAUDE.md 时误以为含元字符就一定要派子 agent。

## 扩展放行：stdout filter argv0 allowlist

光放 fd-to-fd 复制还不够。用户的真实痛点完整长这样：`make logs APP=channel-proxy LANE=coe-t5-id SINCE=10m 2>&1 | tail -150`。前半段 `2>&1` 的语义问题靠上面的 fd 放行解决；后半段 `| tail -150` 是另一个问题——`tail` 当前根本不在 argv0 allowlist 里，hook 拦的不是 `>` 而是"未审计 argv0"。所以解决用户痛点必须把常见的 stdout filter argv0 一起放进 allowlist，否则 fd 放行单独做了等于没做。

加进 argv0 allowlist 的 stdout filter：`tail`、`head`、`grep`、`awk`、`sed`、`cut`、`sort`、`uniq`、`wc`、`jq`、`column`。这一组的共同特征是从 stdin 读、向 stdout 写、不交互、不写文件、不构造新命令，纯输出整形工具。

明确不放进 allowlist 的几个工具：`tee` 会写文件，落到"文件重定向"语义里，不放；`less` 和 `more` 是交互式终端工具，在 Claude 子进程里跑会卡住或行为诡异，不放；`xargs` 把 stdin 切成 argv 拼成新命令执行，等于任意命令构造，是典型 Trojan 通道，不放。

A 方案承担的 surface 必须明示：放完 `tail/head/grep/awk/sed/...` 之后，主会话用 `tail CLAUDE.md` 或 `grep -r foo .` 不再被 hook 拦，因为 hook 不做"argv 是否含 repo 路径"的精确判定（要做就要解析引号、变量展开、相对路径，工程量和误判面都不划算）。这是 A 方案刻意接受的 trade-off，理由有三条。一是这组 filter 是 read-only，看一眼输出不会污染仓库，没有副作用残留。二是输出量级远小于 Read 工具，`tail -150 CLAUDE.md` 进上下文的最多 150 行，远小于 Read 整文件，对"上下文隔离"铁律的本意冲击有限。三是铁律本意是防代码探索污染主会话上下文（feedback_file_ops_must_use_subagent 里说的就是 Explore / 多文件浏览这种场景），临时整形日志输出不构成代码探索。剩下的风险靠 CLAUDE.md 规范文字约束——如果用户故意 `tail CLAUDE.md` 看仓库代码，那是手贱不是攻击面，不靠 hook 拦。

## 调用方全覆盖

Hook 的唯一调用方是 Claude Code harness 的 PreToolUse 事件钩子，每次主会话或子 agent 发起 Bash / Read / Edit / Write 等工具调用前都会执行一次。直接消费者只有主会话和子 agent 两类。测试调用方只有 `.claude/hooks/test-enforce-routing.sh` 一个。CLAUDE.md "编排豁免边界"段是文档层面的语义来源，需要同步。

不存在间接消费者（hook 不被任何 CI、任何业务代码、任何 skill 脚本调用）。改动只影响主会话发起的 Bash 命令在元字符判定上的行为，子 agent 路径完全不动，安全层完全不动，文件工具铁律完全不动。

## 数据 & 部署影响

零 DB / Redis / MQ / 服务部署改动。影响范围只在本仓库三个文件：`.claude/hooks/enforce-routing.sh`、`.claude/hooks/test-enforce-routing.sh`、`CLAUDE.md`。hook 是 PreToolUse 每次执行 bash 脚本，改完下次主会话调用 Bash 时立即生效，不需要重启 Claude Code、不需要重启会话、不需要部署任何服务。

## 粗颗粒 Task

**T1 — 放行 fd 描述符复制 + 扩展 stdout filter argv0 allowlist**
目标：让 fd-to-fd 复制（`[n]>&[m]`，m 是数字）不再触发走私拦截；同时把 `tail / head / grep / awk / sed / cut / sort / uniq / wc / jq / column` 这一组 stdout filter argv0 加进 allowlist；其他形式继续拦。
产出：`.claude/hooks/enforce-routing.sh` 走私检测段（约行 249-342）的判定逻辑改动 + argv0 allowlist 扩展。
验收清单：
- 原痛点命令 `make logs APP=channel-proxy LANE=coe-t5-id SINCE=10m 2>&1 | tail -150` 在主会话路径下完整放行。
- fd-to-fd 复制 `2>&1`、`1>&2`、`2>&3`、`3<&0` 在主会话路径下放行。
- `&>` / `&>>` / `>&out`（右侧非数字）/ `2>file` / `> /path` / `>> ./foo` / `>(cmd)` / `<(cmd)` 在主会话路径下继续拦。
- `tail -150`、`head -100`、`grep pattern`、`awk '{print $1}'`、`sed s/x/y/`、`jq .` 这一组作为 pipe segment 的 argv0 在主会话路径下放行。
- `tee /tmp/x`、`less foo`、`more bar`、`xargs rm` 作为 pipe segment 在主会话路径下继续拦。
- 走私通道 `git status > CLAUDE.md`、`grep -r foo < src/x.py`、`git status $(cat CLAUDE.md)`、` cat \`whoami\` ` 在主会话路径下继续拦。

**T2 — 补单元测试**
目标：把"哪些放 / 哪些拦"的边界用测试钉死，防止后续回归。特别要覆盖 codex T1 review 指出的"安全 fd 后接危险尾巴"组合形态——单看 `2>&1` 是放行的，但拼上 `> out` 或 `&& cat <repo 文件>` 就必须拦，hook 不能因为前缀安全就放过整串。
产出：`.claude/hooks/test-enforce-routing.sh` 新增 case，必须覆盖以下分组：

组合形态（codex 建议）：`make logs 2>&1 > out` 拦（结尾 `> out` 写文件）、`make logs 2>&1 && cat CLAUDE.md` 拦（`cat` 读 repo 路径，argv0 / 路径都不在允许范围）、`make logs 2>&1 | tail -150` 放。

fd 边界：`2>&1` 放、`1>&2` 放、`3<&0` 放、`&>` 拦、`&>>` 拦、`>&out` 拦（右侧非数字 token）、`2>file` 拦、`>(cmd)` 拦、`<(cmd)` 拦。

stdout filter argv0：`tail -150` 放、`head -100` 放、`grep pattern` 放、`awk '{print $1}'` 放（注意 awk pattern 含 `$1`，spec 测试中要单引号包裹避免 shell 展开）、`sed s/x/y/` 放、`jq .` 放。

危险 argv0：`tee /tmp/x` 拦、`less foo` 拦、`more bar` 拦、`xargs rm` 拦。

验收：跑 `bash .claude/hooks/test-enforce-routing.sh` 全部 pass，case 数量较改前显著增加且新 case 与上述四组场景一一对应。

**T3 — 同步 CLAUDE.md 表述**
目标：澄清新边界，避免文档和 hook 行为脱节。
产出：CLAUDE.md "编排豁免边界"段（行 134 附近）补一句话，要点是 fd-to-fd 复制（`[n]>&[m]`，m=数字）以及 stdout filter argv0（`tail` / `head` / `grep` / `awk` / `sed` / `cut` / `sort` / `uniq` / `wc` / `jq` / `column`）在主会话可直接做；文件重定向（`>` / `>>` / `&>` / `&>>` / `>&file`）、命令替换（`$(...)` / 反引号）、输入重定向（`<` / `<<<` / `<(`）、进程替换（`>(cmd)`）、`tee` / `xargs` / `less` 这些必须子 agent。
验收：用户人工 review 这一段表述清晰、与 hook 行为一致。
