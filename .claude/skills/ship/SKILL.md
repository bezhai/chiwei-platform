---
name: ship
description: 合并当前分支到 main 并部署到生产环境
user_invocable: true
---

# /ship

一键完成：PR → 合码 → 部署生产。用户敲 `/ship` 即表示授权合并和部署。

合码规则（等用户确认、列出所有改动、冲突展示）的单一事实来源是 `.claude/rules/merge-and-ship.md`，本文件只描述执行流程。

## 参数

```
!`echo "$ARGUMENTS"`
```

可选传入 `APP`。不传则自动检测。

## 执行流程

### 1. 自动检测

- **APP**: 从参数获取，或从 `git diff --name-only main...HEAD` 检测。涉及多个 app 且未指定时才问。如果只改了根目录文件（Makefile、CLAUDE.md 等），标记为"仅合码，无需部署"。
- **分支**: `git branch --show-current`，禁止在 main 上执行。

### 2. 检查是否有不属于当前工作的改动

对比分支上的全部改动文件（`git diff --name-only main...HEAD`）和当前对话中实际修改过的文件。如果发现有**不是当前对话改动的文件**（如之前遗留的 commit），必须提醒用户：

> 分支上还有以下不属于本次改动的文件：xxx，是否一起合入？

等用户确认后再继续。如果所有改动都是当前对话的工作范围内，直接继续。

### 3. 自动处理脏状态

不要问，直接做：

```bash
# 有未提交改动 → 自动 commit
git add -A && git commit -m "wip: auto commit before ship"

# 未推送 → 自动 push
git push -u origin <branch>
```

### 4. 创建 PR 并合码

```bash
# 创建 PR（已存在则跳过）
ghc pr create --fill 2>/dev/null || true

# 合码（不带 --delete-branch，避免 worktree 下切 main 失败）
ghc pr merge --squash

# 删除远端分支（合码成功后）
git push origin --delete <branch>
```

**禁止使用 `--delete-branch`**：在 worktree 中该参数会尝试本地切换到 main，导致 `致命错误：'main' 已经检出到 ...`。PR 实际已合并但命令报错，模型会误以为失败而重试。

**合并冲突处理**：遇到冲突时，展示冲突文件和冲突内容，等用户指示如何解决。解决后重新 push 并重试。不要自行选择保留哪个版本。

### 5. 部署

如果标记为"无需部署"，跳到步骤 6。

**必须在主仓库的 main 分支执行部署**。通过 `git worktree list` 找到主仓库路径（bare 或 main worktree）。

```bash
cd <主仓库路径>
git checkout main && git pull
```

根据 APP 类型：
- `paas-engine`: `make self-deploy GIT_REF=main`
- 其他: `make deploy APP=<APP> GIT_REF=main`

超时 10 分钟。

**一镜像多服务自动同步**：`make deploy` / `make release` 会按 Makefile 的 `SIBLINGS` 映射自动同步 release sibling 服务（channel-server → chat-response-worker，lark-service → lark-outbound），无需手动操作。agent-service 已无 sibling（vectorize-worker 随 v4 记忆整机删除）。

### 6. 清理当前分支的测试泳道

只清理**当前分支对应的泳道**（与 deploy-test 相同的推导：`ppe-` + 分支名小写、`/` → `-`、截前 16 字符；或显式传入的 `LANE`），不要动其他泳道。

```bash
make undeploy APP=<APP> LANE=<当前分支对应的泳道名>
```

执行 `/ops unbind bot dev` 解绑 dev bot。

如果该泳道不存在则跳过，不报错。

### 7. 验证并输出

执行 `/ops pods <APP>` 确认 pod Running。

一行总结：`✅ <APP> 已部署到生产环境，镜像: <version>`
