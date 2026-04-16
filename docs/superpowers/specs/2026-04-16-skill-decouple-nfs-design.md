# Skill 定义与仓库解耦 — NFS 共享存储方案

## 背景

当前 agent-service 的 skill 定义（SKILL.md + 脚本）硬编码在代码仓库 `app/skills/definitions/` 中，sandbox-worker 在 Docker 构建时 COPY 这些文件到镜像内。增删改 skill 需要改代码 + 重新构建两个镜像。

**目标**：skill 定义存到外部共享存储，通过 Dashboard UI 管理，agent-service 和 sandbox-worker 运行时从挂载路径读取，支持热加载。

## 架构总览

```
Dashboard (RW)                agent-service (RO)           sandbox-worker (RO)
    │                              │                            │
    └──── /data/skills ────────────┘                            │
              │                                                 │
              ├── bangumi/SKILL.md                              │
              ├── bangumi/scripts/bangumi.py                    │
              ├── ...                                           │
              │                                                 │
    NFS PVC (pvc-shared-skills) ─── /sandbox/skills ────────────┘
              │
    NFS Server (infra node: 10.37.18.206)
              │
    /data00/k8s-volumes/shared-skills/
```

## §1 存储层 — NFS Server + PV/PVC

### NFS Server

在 infra 节点（n37-018-206, IP 10.37.18.206）搭建：

- Export 路径：`/data00/k8s-volumes/shared-skills/`
- Export 配置：`/data00/k8s-volumes/shared-skills *(rw,sync,no_subtree_check,no_root_squash)`
- 初始内容：从 `apps/agent-service/app/skills/definitions/` 复制现有 4 个 skill

### K8s 资源

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-shared-skills
spec:
  capacity:
    storage: 1Gi
  accessModes: [ReadWriteMany]
  nfs:
    server: 10.37.18.206
    path: /data00/k8s-volumes/shared-skills

---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-shared-skills
  namespace: prod
spec:
  accessModes: [ReadWriteMany]
  resources:
    requests:
      storage: 1Gi
  volumeName: pv-shared-skills
```

### 挂载目标

| 服务 | 挂载路径 | 读写 |
|---|---|---|
| agent-service | `/data/skills` | ReadOnly |
| sandbox-worker | `/sandbox/skills` | ReadOnly |
| monitor-dashboard | `/data/skills` | ReadWrite |

## §2 PaaS Engine Volume Mount 支持

### Domain 层

App struct 新增 `Volumes` 字段：

```go
type VolumeMount struct {
    PVCName   string `json:"pvc_name"`
    MountPath string `json:"mount_path"`
    ReadOnly  bool   `json:"read_only"`
    SubPath   string `json:"sub_path,omitempty"`
}

type App struct {
    // ... 现有字段 ...
    Volumes []VolumeMount `json:"volumes,omitempty"`
}
```

### DB Model

AppModel 新增 `Volumes string` 字段（JSON 序列化），GORM AutoMigrate 自动加列。

### Deployer 改动

`applyDeployment()` 中从 `app.Volumes` 生成 K8s Volume + VolumeMount：

- 每个 `VolumeMount` 条目生成一对 `corev1.Volume`（PVC 类型）和 `corev1.VolumeMount`
- Volume name 使用 PVC 名称（去重：同一 PVC 只生成一个 Volume，可有多个 VolumeMount）
- VolumeMount 只加到主容器（sidecar 是 lane-routing proxy，不需要访问 skill 文件）

### App API

PUT `/api/paas/apps/{app}` 已支持 merge 语义，新字段直接可用：

```json
PUT /api/paas/apps/agent-service
{
  "volumes": [
    {"pvc_name": "pvc-shared-skills", "mount_path": "/data/skills", "read_only": true}
  ]
}
```

### 不做的事

- 不做 VolumeConfig/StorageBundle 通用抽象——当前只有一个 PVC 场景
- 不做 PVC 生命周期管理——PVC 手动创建一次，PaaS Engine 只负责挂载

## §3 Dashboard Skill 文件管理

### 后端 API

直接操作 `/data/skills/` 挂载目录，不需要数据库表：

| 接口 | 方法 | 说明 |
|---|---|---|
| `/dashboard/api/skills` | GET | 列出所有 skill（扫描子目录，返回 name + description） |
| `/dashboard/api/skills/:name` | GET | 获取某 skill 的完整内容（SKILL.md + 脚本文件列表） |
| `/dashboard/api/skills/:name` | PUT | 更新 skill（覆盖 SKILL.md 和/或脚本文件） |
| `/dashboard/api/skills/:name` | DELETE | 删除整个 skill 目录 |
| `/dashboard/api/skills` | POST | 创建新 skill（建目录 + 写 SKILL.md） |
| `/dashboard/api/skills/:name/files/*path` | GET | 读取 skill 下某个文件的原始内容 |
| `/dashboard/api/skills/:name/files/*path` | PUT | 写入/更新 skill 下某个文件 |
| `/dashboard/api/skills/:name/files/*path` | DELETE | 删除 skill 下某个文件 |

列表接口返回示例：

```json
[
  {
    "name": "bangumi",
    "description": "搜索 Bangumi 上的动画、书籍、游戏等 ACG 条目",
    "files": ["SKILL.md", "scripts/bangumi.py"]
  }
]
```

### 前端 UI

左右两栏布局：

- **左栏**：skill 列表（卡片或表格），name + description，操作按钮（编辑、删除、新建）
- **右栏**：选中 skill 后展示文件树 + 代码编辑器
  - 文件树：SKILL.md 和 scripts/ 下的文件
  - 编辑器：`@monaco-editor/react`，支持 Markdown 和 Python 语法高亮
  - 保存按钮：调 PUT API 写回

### 不做的事

- 不做版本历史
- 不做权限控制（Dashboard 已有 API Key 认证）
- 不做 SKILL.md 格式校验

## §4 Agent-Service Skill 加载重构

### 启动加载

`main.py` 中改为从环境变量读取路径：

```python
# 优先读挂载路径，fallback 到本地（开发环境）
skills_dir = Path(os.environ.get("SKILLS_DIR", "app/skills/definitions"))
SkillRegistry.load_all(skills_dir)
```

### 热加载

新增后台协程，定期扫描目录变更：

```python
async def _skill_reload_loop(skills_dir: Path, interval: int = 30):
    """每 30 秒检查 skill 文件变更，有变化则重新加载。"""
    last_snapshot = _take_snapshot(skills_dir)  # {path: mtime}
    while True:
        await asyncio.sleep(interval)
        current = _take_snapshot(skills_dir)
        if current != last_snapshot:
            SkillRegistry.load_all(skills_dir)
            last_snapshot = current
```

在 `lifespan()` 中启动，与 MQ consumer 同级。

### 不变的部分

- `loader.py`、`renderer.py`、`sandbox_client.py` 不动
- `agent/tools/skill.py`（load_skill 工具）不动
- `pipeline.py` 中 `SkillRegistry.list_descriptions()` 不动

### Sandbox-Worker

Dockerfile 删掉 COPY 行：

```dockerfile
# 删掉
COPY apps/agent-service/app/skills/definitions /sandbox/skills/
```

代码零改动，`SKILLS_DIR` 环境变量指向的路径不变（`/sandbox/skills`）。

## §5 部署顺序与迁移计划

### 执行步骤

1. **搭建 NFS Server**（infra 节点）
   - 安装 nfs-kernel-server
   - 配置 export
   - 复制现有 4 个 skill 定义

2. **创建 K8s PV + PVC**（手动一次性 kubectl apply）

3. **PaaS Engine 改造 + 部署**
   - Domain/Model 加 Volumes 字段
   - Deployer 加 Volume/VolumeMount 逻辑
   - `make self-deploy`

4. **配置 App 挂载**
   - PUT agent-service → `volumes: [{pvc_name: "pvc-shared-skills", mount_path: "/data/skills", read_only: true}]`
   - PUT sandbox-worker → `volumes: [{pvc_name: "pvc-shared-skills", mount_path: "/sandbox/skills", read_only: true}]`
   - PUT monitor-dashboard → `volumes: [{pvc_name: "pvc-shared-skills", mount_path: "/data/skills", read_only: false}]`

5. **Agent-Service 改造 + 部署**
   - main.py 改加载路径 + 热加载协程
   - 部署 + sync workers

6. **Sandbox-Worker 改造 + 部署**
   - Dockerfile 删 COPY 行
   - 部署

7. **Dashboard 改造 + 部署**
   - 后端 skill 文件 CRUD API
   - 前端 skill 管理 UI
   - 部署 + sync web

8. **验证**
   - Dashboard 编辑 skill → agent-service 热加载生效
   - 飞书调用 load_skill → sandbox-worker 执行脚本正常
   - 新建 skill → 全链路可用

9. **清理**
   - 删除 `agent-service/app/skills/definitions/` 目录

### 回滚策略

- 步骤 3-4：PaaS Engine 回滚，App 无 volumes 字段时 Deployer 忽略，不影响现有部署
- 步骤 5-6：回滚到旧版本，SKILLS_DIR 默认值兜底读镜像内 definitions/
- 步骤 7：Dashboard 回滚不影响 skill 运行
