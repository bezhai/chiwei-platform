# Bangumi Archive 数据同步

## 功能概述

新增的 Bangumi Archive 数据同步功能，每周三上午 7:00 自动执行，从 [Bangumi Archive](https://github.com/bangumi/Archive) 项目下载最新的数据并导入到本地 MongoDB 数据库。

## 执行流程

1. **获取下载链接**: 从 GitHub API 获取 `bangumi/Archive` 项目的最新发布版本
2. **下载数据包**: 下载对应的 ZIP 压缩包到临时目录
3. **解压数据**: 解压 ZIP 文件，获取其中的 `.jsonlines` 文件
4. **数据导入**: 将 jsonlines 文件批量导入到 MongoDB 对应集合中
5. **清理文件**: 删除临时下载的文件

## 定时配置

- **执行时间**: 每周三上午 7:00 (`0 7 * * 3`)
- **重试机制**: 失败时会自动重试 3 次，重试间隔分别为 1秒、5秒、15秒

## 数据存储

### 集合命名规则

根据 jsonlines 文件名自动创建对应的 MongoDB 集合：

- `subject.jsonlines` → `bangumi_archive_subjects`
- `character.jsonlines` → `bangumi_archive_characters`
- `person.jsonlines` → `bangumi_archive_persons`
- `episode.jsonlines` → `bangumi_archive_episodes`
- 等等...

### 批量导入

- 使用批量插入（1000 条/批次）提高导入效率
- 支持大文件处理，逐行解析避免内存溢出
- 错误处理：单行解析错误不会中断整个导入过程

## 技术实现

### 核心文件

- `src/service/bangumiArchiveService.ts` - 主要服务实现
- `src/index.ts` - 定时任务注册

### 依赖包

- `adm-zip` - ZIP 文件解压
- `axios` - HTTP 请求
- `mongodb` - 数据库操作
- `node-cron` - 定时任务调度

### 环境要求

- Node.js 22+
- MongoDB 连接配置
- 网络访问 GitHub API

## 日志输出

执行过程中会输出详细的日志信息：

```
开始执行 Bangumi Archive 数据同步任务...
临时目录已创建: /tmp/bangumi-archive-xxxxx
获取到下载链接: https://github.com/bangumi/Archive/releases/download/...
ZIP 文件已下载: /tmp/bangumi-archive-xxxxx/archive.zip
ZIP 文件已解压到: /tmp/bangumi-archive-xxxxx/extracted
找到 8 个 jsonlines 文件: subject.jsonlines, character.jsonlines, ...
开始处理文件 subject.jsonlines -> 集合 bangumi_archive_subjects
文件大小: 717.00 MB
bangumi_archive_subjects: 已处理 1000 条记录
bangumi_archive_subjects: 已处理 2000 条记录
...
所有 jsonlines 文件处理完成
临时文件已清理
```

## 注意事项

1. **存储空间**: Bangumi Archive 数据量较大（约 1.3GB+），确保有足够的存储空间
2. **网络稳定**: 下载过程需要稳定的网络连接
3. **执行时间**: 完整导入可能需要较长时间，建议在低峰期执行
4. **集合独立**: 新数据会写入独立的集合，不会与现有数据冲突

## 手动执行

如需手动执行同步任务：

```javascript
import { syncBangumiArchive } from './src/service/bangumiArchiveService';

// 手动执行同步
await syncBangumiArchive();
```
