---
description: 快速拉服务日志
user_invocable: true
---

# /ops-logs

拉取指定服务的运行日志。

## 预处理数据

```
!`bash .claude/skills/ops-logs/logs.sh $ARGUMENTS`
```

## 指令

1. 展示上面的日志输出
2. 高亮标注 ERROR、WARN、Exception、Traceback 等关键信息
3. 如有明显错误模式，给出简短分析
