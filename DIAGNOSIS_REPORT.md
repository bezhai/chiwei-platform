# Version 字段保存失败 - 完整诊断报告

## 问题症状
- API 不返回 `version` 字段
- 即使发送了 `"version": "xxx"` 参数，数据库中也不保存

## 诊断结果

### 第一阶段：代码验证 ✅
所有代码修改都已正确提交：
- ✅ `internal/domain/release.go` 有 `Version` 字段（line 24）
- ✅ `internal/service/release_service.go` 的 `CreateReleaseRequest` 有 `Version` 字段
- ✅ `internal/adapter/repository/model.go` 的 `ReleaseModel` 有 `Version` 字段（line 68）
- ✅ `internal/adapter/repository/release_repo.go` 的映射函数正确处理 Version（line 125, 147）
- ✅ `internal/service/release_service.go` 的 `CreateOrUpdateRelease` 正确设置 Version
- ✅ `internal/adapter/kubernetes/deployer.go` 正确注入 VERSION 环境变量（line 72-74）

### 第二阶段：编译验证 ✅
- ✅ 本地编译成功：`make build`
- ✅ 二进制文件包含代码：`strings output/paas-engine | grep "json:\"version,omitempty"`

### 第三阶段：数据库验证 ✅
- ✅ 数据库中有 `version` 列：`\d releases` 确认存在

### 第四阶段：部署状态 ❌ **问题所在**

**核心问题：Pod 中的二进制时间戳不匹配**

```
最新提交：
  7f7dfbc (17:14:16) - fix: add Version field to ReleaseModel
  0520dbd (16:56:53) - feat: support custom version injection via Release API

本地编译：
  时��: 2026-02-27 18:04:00

Pod 中的二进制：
  时间: 2026-02-27 09:15 ← 旧版本！
  时间对应: ~11:57之前的某个版本（甚至早于 0bd937f）
```

**这说明：Pod 中运行的是 `9 小时前` 的代码，根本没有 Version 相关的逻辑！**

### 第五阶段：验证 API 实际行为
```
发送请求：
POST /api/v1/releases/
{
  "app_name": "lark-server",
  "lane": "dev",
  "image_tag": "test-tag",
  "version": "test-version-123"  ← 版本参数被忽略
}

API 响应：✅ 返回成功，但没有 version 字段
数据库查询：❌ version 列为空，说明参数没有被保存
```

## 根本原因

**镜像构建/部署流程失败：新的代码没有编译到镜像中**

1. 最新的 Git 提交（7f7dfbc）包含了 Version 字段的完整实现
2. 部署命令触发了镜像构建（BUILD_ID: f43cb63f-70b7-4913-bbed-cfe71e842a38）
3. 构建日志显示"成功"，但实际上：
   - ❌ 镜像中的二进制没有更新（仍然是 09:15）
   - ❌ Pod 拉取新镜像后，二进制时间戳仍然是 09:15

## 可能的原因

1. **Kaniko 构建使用了缓存的代码**
   - Git context 可能指向了旧的 commit
   - Kaniko 缓存层可能包含旧的编译结果

2. **镜像标签混乱**
   - 虽然标签是 `7f7dfbc`，但镜像层可能是旧的

3. **Harbor 推送失败**（无声失败）
   - 构建成功，但镜像没有被正确推送到 Harbor
   - Pod 拉取的是之前的镜像

## 下一步排查方向

### 立即检查
1. 查看 Kaniko Job 的详细日志（特别是 git clone 步骤）
2. 确认 Git context 指向的确实是最新的 commit
3. 验证镜像是否真的推送到了 Harbor
4. 检查 Pod 使用的 imagePullPolicy（当前：IfNotPresent）

### 快速修复方案
1. **强制重新构建**：删除 Kaniko 缓存，重新构建镜像
2. **改变 imagePullPolicy**：改为 `Always`，强制每次都拉取新镜像
3. **手动验证镜像**：进入 Harbor，检查镜像的实际内容

### 长期改进
1. 为 paas-engine 实施 CI/CD（自动编译、推送、部署）
2. 添加镜像内容验证步骤（确认二进制时间戳）
3. 在部署失败时返回更详细的错误信息

## 测试数据
```bash
# 创建 Release 请求
curl -X POST http://paas-engine:8080/api/v1/releases/ \
  -H "X-API-Key: $API_TOKEN" \
  -d '{
    "app_name": "lark-server",
    "lane": "dev",
    "image_tag": "test-tag",
    "version": "test-version-123"
  }'

# 响应（缺少 version 字段）
{
  "id": "70e1876f-3b24-44b0-aff3-7e14d27716e9",
  "app_name": "lark-server",
  "lane": "dev",
  ...
  # ❌ 没有 version 字段
}

# 数据库查询结果
SELECT version FROM releases WHERE id='70e1876f-...';
# ❌ NULL (空值)
```

## 总结

| 检查项 | 状态 | 备注 |
|--------|------|------|
| Git 提交 | ✅ | 7f7dfbc 和 0520dbd 都已提交 |
| 代码逻辑 | ✅ | Version 字段完整实现 |
| 本地编译 | ✅ | 编译成功，二进制包含代码 |
| 数据库模式 | ✅ | 有 version 列 |
| 镜像构建 | ❌ | 新的代码没有编译进镜像 |
| Pod 部署 | ❌ | Pod 使用的仍然是旧二进制 |
| API 功能 | ❌ | version 参数被忽略，不保存 |

**结论：问题在镜像构建/部署流程，而不在代码或逻辑上。**
