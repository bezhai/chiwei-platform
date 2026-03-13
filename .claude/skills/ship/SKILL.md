---
description: 合并当前分支到 main 并部署到生产环境
user_invocable: true
---

# /ship

创建 PR、合码、部署到生产环境的一键流程。

## 用法

```
/ship [APP]
```

- `APP` — 应用名（可选）。如果本次改动只涉及一个 app，自动检测；涉及多个则必须指定。
  - 对于 `paas-engine`，自动使用 `make self-deploy`（蓝绿部署）
  - 对于其他应用，使用 `make deploy APP=<APP> GIT_REF=main`

## 参数

```
!`echo "$ARGUMENTS"`
```

## 指令

按以下步骤执行：

### 1. 前置检查

- 确认当前不在 main 分支（`git branch --show-current`）
- 确认没有未提交的改动，如有则先用 `/commit` 提交
- 确认已推送到远端（`git push`）

### 2. 检测改动涉及的 APP

运行 `git diff --name-only main...HEAD` 检查改动文件：
- 如果只涉及 `apps/<APP>/`，自动识别 APP
- 如果涉及根目录文件（如 Makefile、CLAUDE.md），不需要部署 APP，仅合码
- 如果涉及多个 app 且未指定参数，询问用户

### 3. 创建 PR

用 `ghc pr create` 创建 PR（如果已存在则跳过）：
- title: 从 commit 历史中提取，保持简洁（<70 字符）
- body: 包含 Summary（改动要点）和 Test plan

### 4. 合码

```bash
ghc pr merge <PR_NUMBER> --squash
```

如果有合并冲突，解决冲突后重新推送并重试。

### 5. 部署

**必须在主仓库执行部署**（路径: 当前 worktree 路径去掉 `-worktrees/<name>` 后缀，或通过 `git worktree list` 获取主仓库路径）。

```bash
cd <主仓库路径>
git pull
```

然后根据 APP 类型：
- `paas-engine`: `make self-deploy GIT_REF=main`
- 其他 APP: `make deploy APP=<APP> GIT_REF=main`
- 无需部署（仅根目录文件改动）: 跳过此步

超时上限 10 分钟。

### 6. 验证

部署完成后：
- `kubectl -n prod get pods -l app=<APP>` 确认 pod Running
- 展示部署结果

### 7. 清理测试泳道（如有）

检查是否有该 APP 的非 prod/blue 泳道残留：
```bash
make status APP=<APP>
```

如有测试泳道，提示用户是否清理。

## 注意

- 部署命令**必须在主仓库的 main 分支执行**，不能在 worktree 中执行
- GIT_REF 必须显式写 `main`
- 合并后如果不需要部署（如只改了文档），直接跳到清理步骤
