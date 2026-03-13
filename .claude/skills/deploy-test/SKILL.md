---
description: 将当前分支部署到测试泳道，验证改动
user_invocable: true
---

# /deploy-test

将当前分支构建并部署到测试泳道进行验证。

## 用法

```
/deploy-test APP [LANE]
```

- `APP` — 应用名（必填），如 `paas-engine`、`agent-service`
- `LANE` — 泳道名（可选），默认从分支名生成

## 参数

```
!`echo "$ARGUMENTS"`
```

## 指令

按以下步骤执行：

### 1. 解析参数

从上面的参数中提取 `APP` 和 `LANE`。如果参数为空或缺少 APP，提示用法并停止。

LANE 默认规则：取当前分支名，将 `/` 替换为 `-`，截取前 20 个字符。

### 2. 前置检查

- 确认当前不在 main 分支上（`git branch --show-current`），main 分支禁止泳道测试部署
- 确认没有未提交的改动（`git status --porcelain`），如有则先用 `/commit` 提交

### 3. 推送到远端

```bash
git push -u origin <branch>
```

Kaniko 从 git remote 拉代码，本地 commit 不够。

### 4. 部署

```bash
make deploy APP=<APP> GIT_REF=<branch> LANE=<LANE>
```

超时上限 5 分钟。如果构建或部署失败，展示错误信息并停止。

### 5. 验证

部署成功后：

1. `kubectl -n prod get pods -l app=<APP>,lane=<LANE>` 确认 pod Running
2. 输出泳道访问方式：通过 `$PAAS_API` + `x-lane: <LANE>` header 访问

### 6. 总结

输出一行总结：`<APP> 已部署到 <LANE> 泳道，镜像: <image>`

## 注意

- 部署命令在当前 worktree 执行即可，不需要切到主仓库
- 如果部署失败，用 `make undeploy APP=<APP> LANE=<LANE>` 清理
