---
description: 快速查看集群健康状态，可指定 app 名称深入查看
user_invocable: true
---

# /ops-status

查看 K8s 集群 pod 状态和事件。

## 预处理数据

```
!`kubectl get pods -n prod -o wide --sort-by=.metadata.name 2>&1`
```

```
!`kubectl get events -n prod --sort-by='.lastTimestamp' --field-selector type!=Normal 2>&1 | tail -20`
```

## 指令

1. 分析上面的 pod 状态和异常事件，输出格式化的健康摘要表
2. 标注任何异常状态（CrashLoopBackOff、ImagePullBackOff、Pending、OOMKilled 等）
3. 如果 `$ARGUMENTS` 指定了 app 名称，额外执行：
   - `kubectl describe deployment {app}-prod -n prod`（如果不存在尝试 `{app}`）
   - `kubectl top pods -n prod -l app={app}`
   - 输出该 app 的详细状态、资源用量和最近事件
4. 如果没有异常，简短确认"集群健康"
