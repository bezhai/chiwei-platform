---
description: 合并当前分支到 main 并部署到生产环境
user_invocable: true
---

# /ship

一键完成：PR → 合码 → 部署生产。用户敲 `/ship` 即表示授权合并和部署。

## 参数

```
!`echo "$ARGUMENTS"`
```

可选传入 `APP`。不传则自动检测。

## 执行流程

### 1. 自动检测

- **APP**: 从参数获取，或从 `git diff --name-only main...HEAD` 检测。涉及多个 app 且未指定时才问。如果只改了根目录文件（Makefile、CLAUDE.md 等），标记为"仅合码，无需部署"。
- **分支**: `git branch --show-current`，禁止在 main 上执行。

### 2. 自动处理脏状态

不要问，直接做：

```bash
# 有未提交改动 → 自动 commit
git add -A && git commit -m "wip: auto commit before ship"

# 未推送 → 自动 push
git push -u origin <branch>
```

### 3. 创建 PR 并合码

```bash
# 创建 PR（已存在则跳过）
ghc pr create --fill 2>/dev/null || true

# 直接合码
ghc pr merge --squash --delete-branch
```

合并冲突时：自动 rebase，解决冲突后重新 push 并重试。

### 4. 部署

如果标记为"无需部署"，跳到步骤 5。

**必须在主仓库的 main 分支执行部署**。通过 `git worktree list` 找到主仓库路径（bare 或 main worktree）。

```bash
cd <主仓库路径>
git checkout main && git pull
```

根据 APP 类型：
- `paas-engine`: `make self-deploy GIT_REF=main`
- 其他: `make deploy APP=<APP> GIT_REF=main`

超时 10 分钟。

### 5. 清理当前分支的测试泳道

只清理**当前分支对应的泳道**（即步骤 1 中按分支名生成的 LANE：`/` → `-`，截前 20 字符），不要动其他泳道。

```bash
make undeploy APP=<APP> LANE=<当前分支对应的泳道名>
make lane-unbind TYPE=bot KEY=dev
```

如果该泳道不存在则跳过，不报错。

### 6. 验证并输出

```bash
kubectl -n prod get pods -l app=<APP>
```

一行总结：`✅ <APP> 已部署到生产环境，镜像: <version>`
