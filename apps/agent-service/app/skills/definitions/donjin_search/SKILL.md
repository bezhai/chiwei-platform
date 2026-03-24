---
description: 搜索同人展、漫展、ONLY展等线下活动信息（城市、时间、类型）
---

# donjin-search

## 搜索参数

```
!`python3 $SKILL_DIR/scripts/search.py --help`
```

## 指令

用户想查询同人展/漫展/ONLY展等线下活动。根据用户描述提取搜索条件，用 sandbox_bash 调用搜索脚本。

### 调用方式

```
sandbox_bash("python3 /sandbox/skills/donjin_search/scripts/search.py --query '关键词' --activity-status ongoing")
```

### 常见查询示例

- 查最近的漫展：`--activity-status ongoing`
- 查某城市的活动：`--query '北京'`  或 `--query '上海漫展'`
- 查 ONLY 展：`--activity-type ONLY`
- 查正在售票的：`--ticket-status 3`
- 综合条件：`--query '东方' --activity-type ONLY --activity-status ongoing`

### 结果处理

搜索脚本返回 JSON，包含 `total`（总数）和 `events` 数组。每个 event 包含：
- name（活动名）、type（类型）、tag（标签）
- enter_time / end_time（开始/结束时间）
- city_name / enter_address（城市/地址）
- event_url（活动链接）
- wanna_go_count（想参加人数）

用你自己的风格整理后告诉用户，挑重点信息，不要堆数据。如果有活动链接可以一并给出。
