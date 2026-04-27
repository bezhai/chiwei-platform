---
name: deploy-test
description: 将当前分支部署到测试泳道，验证改动
user_invocable: true
---

# /deploy-test

一键将当前分支部署到测试泳道。

## 参数

```
!`echo "$ARGUMENTS"`
```

可选传入 `APP`。不传则自动从 `git diff --name-only main...HEAD` 检测改动的 `apps/<APP>/`。

## 执行流程

### 1. 自动检测

- **APP**: 从参数获取，或从改动文件自动检测。涉及多个 app 且未指定时才问用户。
- **LANE**: 从当前分支名生成（`/` → `-`，截前 20 字符）。
- **分支**: `git branch --show-current`，禁止在 main 上执行。

### 2. 自动处理脏状态

不要问，直接做：

```bash
# 有未提交改动 → 自动 commit
git add -A && git commit -m "wip: auto commit before deploy-test"

# 未推送 → 自动 push
git push -u origin <branch>
```

### 3. 部署

```bash
make deploy APP=<APP> GIT_REF=<branch> LANE=<LANE>
```

超时 5 分钟。失败则展示错误信息并停止。

### 4. 验证并输出

执行 `/ops pods <APP> <LANE>`，确认 pod Running。

然后执行 `/ops bind bot dev <LANE>` 绑定飞书 dev bot。

输出一行总结：

```
✅ <APP> 已部署到 <LANE> 泳道，dev bot 已绑定
访问: $PAAS_API + header `x-lane: <LANE>`
```
