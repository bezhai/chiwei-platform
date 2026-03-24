---
description: 搜索 Bangumi 上的动画、书籍、游戏等 ACG 条目，查询角色和人物信息
---

# bangumi

## 可用命令

```
!`python3 $SKILL_DIR/scripts/bangumi.py --help`
```

## 指令

用户想查询 ACG（动画、漫画、游戏、轻小说等）相关信息。根据用户描述选择合适的子命令，用 sandbox_bash 调用脚本。

### 搜索条目

查动画/书籍/游戏等作品：

```
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py search-subjects --keyword '关键词' --types 动画")
```

常用参数组合：
- 按类型：`--types 动画 书籍`（可多选）
- 按评分：`--min-rating 8`（8分以上）
- 按日期：`--start-date 2024-01-01`
- 按热度排序：`--sort heat`
- 标签筛选：`--tags 科幻 机战`

### 搜索角色

```
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py search-characters --keyword '角色名'")
```

### 搜索人物（声优、漫画家等）

```
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py search-persons --keyword '人名' --careers 声优")
```

可选职业：制作人员、漫画家、音乐人、声优、作家、绘师、演员

### 获取详情

```
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-subject --id 12345")
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-character --id 12345")
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-person --id 12345")
```

### 查询关联数据

查看条目的角色、制作人员、关联作品：

```
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-related --entity subject --id 12345 --relation characters")
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-related --entity subject --id 12345 --relation persons")
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-related --entity subject --id 12345 --relation relations")
```

查看角色出演的作品、声优：

```
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-related --entity character --id 12345 --relation subjects")
sandbox_bash("python3 $SKILL_DIR/scripts/bangumi.py get-related --entity character --id 12345 --relation persons")
```

### 典型查询流程

1. 先 search 找到目标的 ID
2. 再用 get-subject/get-character/get-person 获取详情
3. 需要关联信息时用 get-related

### 结果处理

脚本返回 JSON。用你自己的风格整理后告诉用户，挑重点信息。给出 Bangumi 链接格式：`https://bgm.tv/subject/{id}`
