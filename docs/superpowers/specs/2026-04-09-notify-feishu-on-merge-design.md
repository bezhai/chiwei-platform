# PR 合并飞书通知

## 背景

PR 合并到 main 后缺乏即时通知，需要手动检查 GitHub 才能知道合码状态。希望合并后自动往飞书群发一条通知。

## 方案

GitHub Actions workflow，监听 PR 合并事件，通过飞书 webhook 发送卡片消息。

## 触发条件

- 事件：`pull_request` → `closed`
- 条件：`github.event.pull_request.merged == true`
- 目标分支：`main`

## 消息内容

| 字段 | 来源 |
|------|------|
| PR 标题 | `github.event.pull_request.title` |
| PR 编号 | `github.event.pull_request.number` |
| PR 链接 | `github.event.pull_request.html_url` |
| 改动核心内容 | PR body 中 `## Summary` 段落；fallback 到 body 前 5 行 |

## 消息格式

飞书 webhook interactive card：

```json
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": { "tag": "plain_text", "content": "[PR #编号] 标题" },
      "template": "green"
    },
    "elements": [
      {
        "tag": "markdown",
        "content": "Summary 内容"
      },
      {
        "tag": "action",
        "actions": [
          {
            "tag": "button",
            "text": { "tag": "plain_text", "content": "查看 PR" },
            "url": "PR 链接",
            "type": "primary"
          }
        ]
      }
    ]
  }
}
```

## 安全

- Webhook URL 作为 GitHub repo secret：`FEISHU_MERGE_WEBHOOK_URL`
- Workflow 通过 `${{ secrets.FEISHU_MERGE_WEBHOOK_URL }}` 引用
- 不在代码中硬编码任何 URL

## 文件变更

- 新增：`.github/workflows/notify-feishu-on-merge.yml`

## Summary 提取逻辑

1. 用 shell 从 PR body 中匹配 `## Summary` 到下一个 `##`（或 EOF）之间的文本
2. 去除首尾空行
3. 如果提取为空，fallback 到 body 前 5 行
4. 如果 body 本身为空，显示"（无描述）"
