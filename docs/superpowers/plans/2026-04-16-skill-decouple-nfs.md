# Skill 定义与仓库解耦 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 agent-service 的 skill 定义（SKILL.md + 脚本）从代码仓库迁移到 NFS 共享存储，通过 Dashboard UI 管理，支持热加载。

**Architecture:** infra 节点搭 NFS Server，创建 RWX PVC。PaaS Engine 新增 VolumeMount 能力，agent-service/sandbox-worker/dashboard 三个服务挂载同一 PVC。Dashboard 直接操作挂载目录做文件 CRUD，agent-service 定期扫描文件变更触发热加载。

**Tech Stack:** Go (PaaS Engine), Python/FastAPI (agent-service, sandbox-worker), TypeScript/Hono (Dashboard backend), React/Ant Design (Dashboard frontend), NFS, K8s PV/PVC

**Spec:** `docs/superpowers/specs/2026-04-16-skill-decouple-nfs-design.md`

---

## File Structure

### PaaS Engine (Go)

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `apps/paas-engine/internal/domain/app.go` | Add `VolumeMount` type and `Volumes` field to `App` |
| Modify | `apps/paas-engine/internal/adapter/repository/model.go` | Add `Volumes string` to `AppModel` |
| Modify | `apps/paas-engine/internal/adapter/repository/app_repo.go` | JSON serialize/deserialize `Volumes` in `appToModel`/`modelToApp` |
| Modify | `apps/paas-engine/internal/service/app_service.go` | Add `ApplyField` for `volumes` in `UpdateApp` |
| Modify | `apps/paas-engine/internal/adapter/kubernetes/deployer.go` | Build K8s Volume + VolumeMount from `app.Volumes` |
| Modify | `apps/paas-engine/internal/adapter/kubernetes/deployer_test.go` | Test volume mount behavior |

### Agent-Service (Python)

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `apps/agent-service/app/main.py` | Read `SKILLS_DIR` env var, start reload loop |
| Modify | `apps/agent-service/app/skills/registry.py` | Add `_take_snapshot` and `start_reload_loop` |
| Modify | `apps/agent-service/tests/unit/test_skill_loader.py` | Test hot-reload behavior |

### Sandbox-Worker

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `apps/sandbox-worker/Dockerfile` | Remove `COPY ... /sandbox/skills/`, keep `mkdir` |

### Dashboard Backend (TypeScript)

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `apps/monitor-dashboard/src/routes/skills.ts` | Skill file CRUD API routes |
| Modify | `apps/monitor-dashboard/src/index.ts` | Register skills routes |

### Dashboard Frontend (React)

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `apps/monitor-dashboard-web/src/pages/Skills.tsx` | Skill management page |
| Modify | `apps/monitor-dashboard-web/src/App.tsx` | Add route + menu item |

---

## Task 1: PaaS Engine — Domain 层添加 VolumeMount

**Files:**
- Modify: `apps/paas-engine/internal/domain/app.go:1-22`

- [ ] **Step 1: Add VolumeMount type and Volumes field**

```go
// apps/paas-engine/internal/domain/app.go
package domain

import "time"

// VolumeMount 描述一个 PVC 到容器路径的挂载。
type VolumeMount struct {
	PVCName   string `json:"pvc_name"`
	MountPath string `json:"mount_path"`
	ReadOnly  bool   `json:"read_only"`
	SubPath   string `json:"sub_path,omitempty"`
}

// App 代表一个应用定义，是 PaaS 引擎的核心管理单元。
// App 本身不映射到任何 K8s 资源，仅作为逻辑锚点。
type App struct {
	Name              string            `json:"name"`
	Description       string            `json:"description,omitempty"`
	ImageRepoName     string            `json:"image_repo"`
	Port              int               `json:"port"`
	ServiceAccount    string            `json:"service_account,omitempty"`
	Command           []string          `json:"command,omitempty"`
	EnvFromSecrets    []string          `json:"env_from_secrets,omitempty"`
	EnvFromConfigMaps []string          `json:"env_from_config_maps,omitempty"`
	Envs              map[string]string `json:"envs,omitempty"`
	ConfigBundles     []string          `json:"config_bundles,omitempty"`
	SidecarEnabled    bool              `json:"sidecar_enabled,omitempty"`
	Volumes           []VolumeMount     `json:"volumes,omitempty"`
	CreatedAt         time.Time         `json:"created_at"`
	UpdatedAt         time.Time         `json:"updated_at"`
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd apps/paas-engine && go build ./...`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add apps/paas-engine/internal/domain/app.go
git commit -m "feat(paas-engine): add VolumeMount type to App domain"
```

---

## Task 2: PaaS Engine — DB Model + Repository 序列化

**Files:**
- Modify: `apps/paas-engine/internal/adapter/repository/model.go:6-22`
- Modify: `apps/paas-engine/internal/adapter/repository/app_repo.go:78-162`

- [ ] **Step 1: Add Volumes field to AppModel**

In `model.go`, add `Volumes` field to `AppModel`:

```go
type AppModel struct {
	Name              string `gorm:"primaryKey"`
	Description       string
	ImageRepoName     string
	Port              int
	ServiceAccount    string
	Command           string // JSON 序列化的 []string
	EnvFromSecrets    string // JSON 序列化的 []string
	EnvFromConfigMaps string // JSON 序列化的 []string
	Envs              string // JSON 序列化的 map[string]string
	ConfigBundles     string // JSON 序列化的 []string
	SidecarEnabled    bool
	Volumes           string // JSON 序列化的 []VolumeMount
	CreatedAt         time.Time
	UpdatedAt         time.Time
}
```

- [ ] **Step 2: Update appToModel — serialize Volumes**

In `app_repo.go` `appToModel` function, add after `configBundlesJSON` serialization (before `return &AppModel{`):

```go
	volumesJSON, err := json.Marshal(a.Volumes)
	if err != nil {
		return nil, err
	}
```

And add to the returned AppModel:

```go
		Volumes:           string(volumesJSON),
```

- [ ] **Step 3: Update modelToApp — deserialize Volumes**

In `app_repo.go` `modelToApp` function, add after `configBundles` deserialization (before `return &domain.App{`):

```go
	var volumes []domain.VolumeMount
	if m.Volumes != "" {
		if err := json.Unmarshal([]byte(m.Volumes), &volumes); err != nil {
			return nil, err
		}
	}
```

And add to the returned App:

```go
		Volumes:           volumes,
```

- [ ] **Step 4: Verify it compiles**

Run: `cd apps/paas-engine && go build ./...`
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add apps/paas-engine/internal/adapter/repository/model.go apps/paas-engine/internal/adapter/repository/app_repo.go
git commit -m "feat(paas-engine): serialize Volumes field in AppModel"
```

---

## Task 3: PaaS Engine — App Service 支持 volumes 更新

**Files:**
- Modify: `apps/paas-engine/internal/service/app_service.go:85-148`

- [ ] **Step 1: Add ApplyField for volumes and add to CreateAppRequest**

In `app_service.go`, add `Volumes` to `CreateAppRequest`:

```go
type CreateAppRequest struct {
	Name              string               `json:"name"`
	Description       string               `json:"description"`
	ImageRepoName     string               `json:"image_repo"`
	Port              int                  `json:"port"`
	ServiceAccount    string               `json:"service_account"`
	Command           []string             `json:"command"`
	EnvFromSecrets    []string             `json:"env_from_secrets"`
	EnvFromConfigMaps []string             `json:"env_from_config_maps"`
	Envs              map[string]string    `json:"envs"`
	ConfigBundles     []string             `json:"config_bundles"`
	Volumes           []domain.VolumeMount `json:"volumes"`
}
```

In `CreateApp`, add before `now := time.Now()`:

(no validation needed — VolumeMount is a simple struct)

And add to the created `domain.App`:

```go
		Volumes:           req.Volumes,
```

In `UpdateApp`, add after `ApplyField(fields, "sidecar_enabled", &app.SidecarEnabled)`:

```go
	if err := ApplyField(fields, "volumes", &app.Volumes); err != nil {
		return nil, domain.ErrInvalidInput
	}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd apps/paas-engine && go build ./...`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add apps/paas-engine/internal/service/app_service.go
git commit -m "feat(paas-engine): support volumes field in App CRUD"
```

---

## Task 4: PaaS Engine — Deployer 生成 Volume/VolumeMount

**Files:**
- Modify: `apps/paas-engine/internal/adapter/kubernetes/deployer.go:189-321`
- Test: `apps/paas-engine/internal/adapter/kubernetes/deployer_test.go`

- [ ] **Step 1: Write the failing test**

In `deployer_test.go`, add:

```go
// TestApplyDeploymentWithVolumes 验证 Volumes 字段生成正确的 K8s Volume + VolumeMount。
func TestApplyDeploymentWithVolumes(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name:           "agent-service",
		Port:           8000,
		EnvFromSecrets: []string{"app-env"},
		Volumes: []domain.VolumeMount{
			{PVCName: "pvc-shared-skills", MountPath: "/data/skills", ReadOnly: true},
		},
	}

	release := &domain.Release{
		ID:       "r1",
		AppName:  "agent-service",
		Lane:     "prod",
		Image:    "harbor.local/inner-bot/agent-service:1.0.0",
		Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "agent-service-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	// 验证 PodSpec.Volumes 包含 PVC volume
	volumes := deploy.Spec.Template.Spec.Volumes
	if len(volumes) != 1 {
		t.Fatalf("expected 1 volume, got %d", len(volumes))
	}
	if volumes[0].Name != "pvc-shared-skills" {
		t.Errorf("volume name = %q, want %q", volumes[0].Name, "pvc-shared-skills")
	}
	if volumes[0].PersistentVolumeClaim == nil {
		t.Fatal("expected PVC volume source, got nil")
	}
	if volumes[0].PersistentVolumeClaim.ClaimName != "pvc-shared-skills" {
		t.Errorf("PVC claim name = %q, want %q", volumes[0].PersistentVolumeClaim.ClaimName, "pvc-shared-skills")
	}
	if !volumes[0].PersistentVolumeClaim.ReadOnly {
		t.Error("expected PVC readOnly = true")
	}

	// 验证主容器有 VolumeMount
	container := deploy.Spec.Template.Spec.Containers[0]
	if len(container.VolumeMounts) != 1 {
		t.Fatalf("expected 1 volumeMount, got %d", len(container.VolumeMounts))
	}
	if container.VolumeMounts[0].Name != "pvc-shared-skills" {
		t.Errorf("volumeMount name = %q, want %q", container.VolumeMounts[0].Name, "pvc-shared-skills")
	}
	if container.VolumeMounts[0].MountPath != "/data/skills" {
		t.Errorf("volumeMount mountPath = %q, want %q", container.VolumeMounts[0].MountPath, "/data/skills")
	}
	if !container.VolumeMounts[0].ReadOnly {
		t.Error("expected volumeMount readOnly = true")
	}
}

// TestApplyDeploymentWithVolumesDedup 验证同一 PVC 多个挂载点时 Volume 去重。
func TestApplyDeploymentWithVolumesDedup(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name: "multi-mount",
		Port: 8000,
		Volumes: []domain.VolumeMount{
			{PVCName: "pvc-shared", MountPath: "/data/a", ReadOnly: true},
			{PVCName: "pvc-shared", MountPath: "/data/b", ReadOnly: false, SubPath: "sub"},
		},
	}

	release := &domain.Release{
		ID: "r1", AppName: "multi-mount", Lane: "prod",
		Image: "img:1", Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "multi-mount-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	// Volume 去重：同一 PVC 只生成一个 Volume
	if len(deploy.Spec.Template.Spec.Volumes) != 1 {
		t.Errorf("expected 1 deduplicated volume, got %d", len(deploy.Spec.Template.Spec.Volumes))
	}

	// VolumeMount 不去重：两个不同挂载点
	container := deploy.Spec.Template.Spec.Containers[0]
	if len(container.VolumeMounts) != 2 {
		t.Fatalf("expected 2 volumeMounts, got %d", len(container.VolumeMounts))
	}
	if container.VolumeMounts[1].SubPath != "sub" {
		t.Errorf("second volumeMount subPath = %q, want %q", container.VolumeMounts[1].SubPath, "sub")
	}
}

// TestApplyDeploymentNoVolumes 验证无 Volumes 时行为不变。
func TestApplyDeploymentNoVolumes(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{Name: "plain-app", Port: 8080}
	release := &domain.Release{
		ID: "r1", AppName: "plain-app", Lane: "prod",
		Image: "img:1", Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "plain-app-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	if len(deploy.Spec.Template.Spec.Volumes) != 0 {
		t.Errorf("expected no volumes, got %d", len(deploy.Spec.Template.Spec.Volumes))
	}
	if len(deploy.Spec.Template.Spec.Containers[0].VolumeMounts) != 0 {
		t.Errorf("expected no volumeMounts, got %d", len(deploy.Spec.Template.Spec.Containers[0].VolumeMounts))
	}
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/paas-engine && go test ./internal/adapter/kubernetes/ -run TestApplyDeploymentWithVolumes -v`
Expected: FAIL — no volumes generated yet

- [ ] **Step 3: Implement volume building in deployer**

In `deployer.go`, add a helper function before `applyDeployment`:

```go
// buildPVCVolumes 从 App.Volumes 构建 K8s Volume 列表和主容器 VolumeMount 列表。
// 同一 PVC 只生成一个 Volume（去重），但允许多个 VolumeMount。
func buildPVCVolumes(appVolumes []domain.VolumeMount) ([]corev1.Volume, []corev1.VolumeMount) {
	if len(appVolumes) == 0 {
		return nil, nil
	}

	seen := make(map[string]bool)
	var volumes []corev1.Volume
	var mounts []corev1.VolumeMount

	for _, v := range appVolumes {
		if !seen[v.PVCName] {
			seen[v.PVCName] = true
			volumes = append(volumes, corev1.Volume{
				Name: v.PVCName,
				VolumeSource: corev1.VolumeSource{
					PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
						ClaimName: v.PVCName,
						ReadOnly:  v.ReadOnly,
					},
				},
			})
		}

		mounts = append(mounts, corev1.VolumeMount{
			Name:      v.PVCName,
			MountPath: v.MountPath,
			ReadOnly:  v.ReadOnly,
			SubPath:   v.SubPath,
		})
	}

	return volumes, mounts
}
```

Then in `applyDeployment`, after `container` is built (after the `if app.Port > 0` block, ~line 237), add:

```go
	// Volume mounts from App.Volumes
	pvcVolumes, pvcMounts := buildPVCVolumes(app.Volumes)
	container.VolumeMounts = pvcMounts
```

And in the `Spec: corev1.PodSpec{...}` block (~line 300-306), add `Volumes`:

```go
				Spec: corev1.PodSpec{
					ServiceAccountName: app.ServiceAccount,
					NodeSelector:       map[string]string{"node-role": "app"},
					InitContainers:     initContainers,
					Containers:         append([]corev1.Container{container}, sidecarContainers...),
					Volumes:            pvcVolumes,
				},
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/paas-engine && go test ./internal/adapter/kubernetes/ -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite**

Run: `cd apps/paas-engine && go test ./... -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add apps/paas-engine/internal/adapter/kubernetes/deployer.go apps/paas-engine/internal/adapter/kubernetes/deployer_test.go
git commit -m "feat(paas-engine): support PVC volume mounts in Deployments"
```

---

## Task 5: Agent-Service — 可配置加载路径 + 热加载

**Files:**
- Modify: `apps/agent-service/app/skills/registry.py:1-79`
- Modify: `apps/agent-service/app/main.py:22-47`
- Test: `apps/agent-service/tests/unit/test_skill_loader.py`

- [ ] **Step 1: Write the failing test for hot-reload**

In `tests/unit/test_skill_loader.py`, add at the end:

```python
class TestSkillReloadLoop:
    def test_take_snapshot_returns_mtime_dict(self, tmp_path):
        from app.skills.registry import SkillRegistry

        skill_dir = tmp_path / "test_skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\ndescription: test\n---\n\n# Test\n"
        )

        snapshot = SkillRegistry.take_snapshot(tmp_path)
        assert len(snapshot) == 1
        assert str(skill_file) in snapshot

    def test_take_snapshot_detects_change(self, tmp_path):
        import time
        from app.skills.registry import SkillRegistry

        skill_dir = tmp_path / "test_skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\ndescription: v1\n---\n\n# V1\n"
        )

        snap1 = SkillRegistry.take_snapshot(tmp_path)

        time.sleep(0.05)
        skill_file.write_text(
            "---\ndescription: v2\n---\n\n# V2\n"
        )

        snap2 = SkillRegistry.take_snapshot(tmp_path)
        assert snap1 != snap2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_skill_loader.py::TestSkillReloadLoop -v`
Expected: FAIL — `SkillRegistry.take_snapshot` does not exist

- [ ] **Step 3: Add take_snapshot to SkillRegistry**

In `app/skills/registry.py`, add to the `SkillRegistry` class:

```python
    @classmethod
    def take_snapshot(cls, skills_dir: Path) -> dict[str, float]:
        """收集所有 SKILL.md 的 mtime，用于变更检测。"""
        snapshot: dict[str, float] = {}
        if not skills_dir.exists():
            return snapshot
        for child in skills_dir.iterdir():
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.exists():
                snapshot[str(skill_file)] = skill_file.stat().st_mtime
                # 也追踪 scripts/ 下的文件
                scripts_dir = child / "scripts"
                if scripts_dir.exists():
                    for script in scripts_dir.iterdir():
                        if script.is_file():
                            snapshot[str(script)] = script.stat().st_mtime
        return snapshot
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_skill_loader.py::TestSkillReloadLoop -v`
Expected: PASS

- [ ] **Step 5: Add reload loop and update main.py**

In `app/skills/registry.py`, add a module-level async function after the class:

```python
async def skill_reload_loop(skills_dir: Path, interval: int = 30) -> None:
    """后台协程：定期检查 skill 文件变更，有变化则重新加载。"""
    last_snapshot = SkillRegistry.take_snapshot(skills_dir)
    while True:
        await asyncio.sleep(interval)
        try:
            current = SkillRegistry.take_snapshot(skills_dir)
            if current != last_snapshot:
                logger.info("Skill files changed, reloading...")
                SkillRegistry.load_all(skills_dir)
                last_snapshot = current
        except Exception as e:
            logger.error("Skill reload check failed: %s", e)
```

Add `import asyncio` to the imports at the top of `registry.py`.

In `app/main.py`, change the skill loading section:

```python
    # Load skill definitions
    import os
    from pathlib import Path

    from app.skills.registry import SkillRegistry, skill_reload_loop

    skills_dir = Path(os.environ.get("SKILLS_DIR", str(Path(__file__).parent / "skills" / "definitions")))
    SkillRegistry.load_all(skills_dir)

    # Start hot-reload loop
    reload_task = asyncio.create_task(skill_reload_loop(skills_dir))
```

And in the shutdown section, cancel the reload task:

```python
    # Cancel reload task
    reload_task.cancel()
    try:
        await reload_task
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 6: Run full test suite**

Run: `cd apps/agent-service && uv run pytest tests/unit/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add apps/agent-service/app/skills/registry.py apps/agent-service/app/main.py apps/agent-service/tests/unit/test_skill_loader.py
git commit -m "feat(agent-service): configurable skills dir + hot-reload"
```

---

## Task 6: Sandbox-Worker — Dockerfile 去掉 COPY

**Files:**
- Modify: `apps/sandbox-worker/Dockerfile:22`

- [ ] **Step 1: Replace COPY with mkdir**

Change line 22 from:

```dockerfile
COPY apps/agent-service/app/skills/definitions /sandbox/skills/
```

To:

```dockerfile
# skill 定义通过 PVC 挂载到 /sandbox/skills/，不再构建时复制
RUN mkdir -p /sandbox/skills
```

- [ ] **Step 2: Verify Dockerfile is valid**

Run: `head -25 apps/sandbox-worker/Dockerfile`
Expected: `RUN mkdir -p /sandbox/skills` on line 22-23

- [ ] **Step 3: Commit**

```bash
git add apps/sandbox-worker/Dockerfile
git commit -m "refactor(sandbox-worker): remove COPY skills, use PVC mount"
```

---

## Task 7: Dashboard Backend — Skill 文件 CRUD API

**Files:**
- Create: `apps/monitor-dashboard/src/routes/skills.ts`
- Modify: `apps/monitor-dashboard/src/index.ts:24,75`

- [ ] **Step 1: Create skills route file**

Create `apps/monitor-dashboard/src/routes/skills.ts`:

```typescript
import { Hono } from 'hono';
import * as fs from 'fs/promises';
import * as path from 'path';

const SKILLS_DIR = process.env.SKILLS_DIR || '/data/skills';

const app = new Hono();

interface SkillSummary {
  name: string;
  description: string;
  files: string[];
}

/** 从 SKILL.md 的 YAML frontmatter 提取 description */
function parseDescription(content: string): string {
  const match = content.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!match) return '';
  const line = match[1].split('\n').find(l => l.startsWith('description:'));
  return line ? line.replace('description:', '').trim() : '';
}

/** 递归收集目录下所有文件的相对路径 */
async function listFilesRecursive(dir: string, base: string = ''): Promise<string[]> {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const rel = base ? `${base}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      files.push(...await listFilesRecursive(path.join(dir, entry.name), rel));
    } else {
      files.push(rel);
    }
  }
  return files;
}

// GET /api/skills — 列出所有 skill
app.get('/api/skills', async (c) => {
  try {
    await fs.access(SKILLS_DIR);
  } catch {
    return c.json([]);
  }

  const entries = await fs.readdir(SKILLS_DIR, { withFileTypes: true });
  const skills: SkillSummary[] = [];

  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const skillDir = path.join(SKILLS_DIR, entry.name);
    const skillFile = path.join(skillDir, 'SKILL.md');
    try {
      const content = await fs.readFile(skillFile, 'utf-8');
      const files = await listFilesRecursive(skillDir);
      skills.push({
        name: entry.name,
        description: parseDescription(content),
        files,
      });
    } catch {
      // 没有 SKILL.md 的目录跳过
    }
  }

  return c.json(skills);
});

// GET /api/skills/:name — 获取某 skill 详情
app.get('/api/skills/:name', async (c) => {
  const skillDir = path.join(SKILLS_DIR, c.req.param('name'));
  try {
    await fs.access(skillDir);
  } catch {
    return c.json({ message: 'Skill not found' }, 404);
  }

  const skillFile = path.join(skillDir, 'SKILL.md');
  const content = await fs.readFile(skillFile, 'utf-8');
  const files = await listFilesRecursive(skillDir);

  return c.json({
    name: c.req.param('name'),
    description: parseDescription(content),
    content,
    files,
  });
});

// POST /api/skills — 创建新 skill
app.post('/api/skills', async (c) => {
  const body = await c.req.json<{ name: string; content: string }>();
  if (!body.name || !body.content) {
    return c.json({ message: 'name and content are required' }, 400);
  }

  const skillDir = path.join(SKILLS_DIR, body.name);
  try {
    await fs.access(skillDir);
    return c.json({ message: 'Skill already exists' }, 409);
  } catch {
    // 不存在，继续创建
  }

  await fs.mkdir(skillDir, { recursive: true });
  await fs.writeFile(path.join(skillDir, 'SKILL.md'), body.content, 'utf-8');
  return c.json({ name: body.name }, 201);
});

// PUT /api/skills/:name — 更新 skill 的 SKILL.md
app.put('/api/skills/:name', async (c) => {
  const skillDir = path.join(SKILLS_DIR, c.req.param('name'));
  try {
    await fs.access(skillDir);
  } catch {
    return c.json({ message: 'Skill not found' }, 404);
  }

  const body = await c.req.json<{ content: string }>();
  await fs.writeFile(path.join(skillDir, 'SKILL.md'), body.content, 'utf-8');
  return c.json({ ok: true });
});

// DELETE /api/skills/:name — 删除整个 skill
app.delete('/api/skills/:name', async (c) => {
  const skillDir = path.join(SKILLS_DIR, c.req.param('name'));
  try {
    await fs.access(skillDir);
  } catch {
    return c.json({ message: 'Skill not found' }, 404);
  }

  await fs.rm(skillDir, { recursive: true });
  return c.json({ ok: true });
});

// GET /api/skills/:name/files/* — 读取 skill 下某个文件
app.get('/api/skills/:name/files/*', async (c) => {
  const filePath = c.req.param('*') || '';
  if (!filePath) {
    return c.json({ message: 'File path required' }, 400);
  }

  const fullPath = path.join(SKILLS_DIR, c.req.param('name'), filePath);

  // 防止路径穿越
  if (!fullPath.startsWith(SKILLS_DIR)) {
    return c.json({ message: 'Invalid path' }, 400);
  }

  try {
    const content = await fs.readFile(fullPath, 'utf-8');
    return c.json({ path: filePath, content });
  } catch {
    return c.json({ message: 'File not found' }, 404);
  }
});

// PUT /api/skills/:name/files/* — 写入 skill 下某个文件
app.put('/api/skills/:name/files/*', async (c) => {
  const filePath = c.req.param('*') || '';
  if (!filePath) {
    return c.json({ message: 'File path required' }, 400);
  }

  const fullPath = path.join(SKILLS_DIR, c.req.param('name'), filePath);

  if (!fullPath.startsWith(SKILLS_DIR)) {
    return c.json({ message: 'Invalid path' }, 400);
  }

  const body = await c.req.json<{ content: string }>();
  await fs.mkdir(path.dirname(fullPath), { recursive: true });
  await fs.writeFile(fullPath, body.content, 'utf-8');
  return c.json({ ok: true });
});

// DELETE /api/skills/:name/files/* — 删除 skill 下某个文件
app.delete('/api/skills/:name/files/*', async (c) => {
  const filePath = c.req.param('*') || '';
  if (!filePath) {
    return c.json({ message: 'File path required' }, 400);
  }

  const fullPath = path.join(SKILLS_DIR, c.req.param('name'), filePath);

  if (!fullPath.startsWith(SKILLS_DIR)) {
    return c.json({ message: 'Invalid path' }, 400);
  }

  try {
    await fs.rm(fullPath);
    return c.json({ ok: true });
  } catch {
    return c.json({ message: 'File not found' }, 404);
  }
});

export default app;
```

- [ ] **Step 2: Register in index.ts**

In `apps/monitor-dashboard/src/index.ts`, add import:

```typescript
import skillsRoutes from './routes/skills';
```

And add route registration after the `dynamicConfigRoutes` line:

```typescript
  dashboard.route('/', skillsRoutes);
```

- [ ] **Step 3: Verify it compiles**

Run: `cd apps/monitor-dashboard && npx tsc --noEmit`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add apps/monitor-dashboard/src/routes/skills.ts apps/monitor-dashboard/src/index.ts
git commit -m "feat(dashboard): skill file CRUD API"
```

---

## Task 8: Dashboard Frontend — Skill 管理页面

**Files:**
- Create: `apps/monitor-dashboard-web/src/pages/Skills.tsx`
- Modify: `apps/monitor-dashboard-web/src/App.tsx`

- [ ] **Step 1: Install Monaco Editor**

Run: `cd apps/monitor-dashboard-web && npm install @monaco-editor/react`

- [ ] **Step 2: Create Skills page**

Create `apps/monitor-dashboard-web/src/pages/Skills.tsx`:

```tsx
import { useEffect, useState, useCallback } from 'react';
import { Card, List, Button, Modal, Input, Space, Typography, message, Popconfirm, Empty, Spin, Tag } from 'antd';
import { PlusOutlined, DeleteOutlined, FileOutlined, CodeOutlined } from '@ant-design/icons';
import Editor from '@monaco-editor/react';
import api from '../api/client';

const { Text, Title } = Typography;

interface SkillSummary {
  name: string;
  description: string;
  files: string[];
}

interface FileContent {
  path: string;
  content: string;
}

function languageForFile(filename: string): string {
  if (filename.endsWith('.md')) return 'markdown';
  if (filename.endsWith('.py')) return 'python';
  if (filename.endsWith('.sh')) return 'shell';
  if (filename.endsWith('.json')) return 'json';
  return 'plaintext';
}

export default function Skills() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string>('SKILL.md');
  const [fileContent, setFileContent] = useState('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');

  const fetchSkills = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get<SkillSummary[]>('/skills');
      setSkills(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchSkills(); }, [fetchSkills]);

  const loadFile = useCallback(async (skillName: string, filePath: string) => {
    try {
      const { data } = await api.get<FileContent>(`/skills/${skillName}/files/${filePath}`);
      setFileContent(data.content);
      setSelectedFile(filePath);
      setDirty(false);
    } catch {
      message.error('Failed to load file');
    }
  }, []);

  useEffect(() => {
    if (selected) {
      loadFile(selected, 'SKILL.md');
    }
  }, [selected, loadFile]);

  const handleSave = async () => {
    if (!selected) return;
    setSaving(true);
    try {
      await api.put(`/skills/${selected}/files/${selectedFile}`, { content: fileContent });
      message.success('Saved');
      setDirty(false);
      fetchSkills();
    } catch {
      message.error('Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleCreate = async () => {
    if (!newName.trim()) return;
    try {
      await api.post('/skills', {
        name: newName.trim(),
        content: `---\ndescription: ${newName.trim()}\n---\n\n# ${newName.trim()}\n`,
      });
      message.success('Created');
      setCreateOpen(false);
      setNewName('');
      fetchSkills();
      setSelected(newName.trim());
    } catch {
      message.error('Create failed');
    }
  };

  const handleDelete = async (name: string) => {
    try {
      await api.delete(`/skills/${name}`);
      message.success('Deleted');
      if (selected === name) {
        setSelected(null);
        setFileContent('');
      }
      fetchSkills();
    } catch {
      message.error('Delete failed');
    }
  };

  const selectedSkill = skills.find(s => s.name === selected);

  return (
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 180px)' }}>
      {/* Left panel: skill list */}
      <Card
        title="Skills"
        style={{ width: 300, flexShrink: 0, overflow: 'auto' }}
        extra={<Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>New</Button>}
      >
        {loading ? <Spin /> : (
          <List
            dataSource={skills}
            locale={{ emptyText: <Empty description="No skills" /> }}
            renderItem={(item) => (
              <List.Item
                style={{
                  cursor: 'pointer',
                  background: selected === item.name ? '#f0f5ff' : undefined,
                  borderRadius: 6,
                  padding: '8px 12px',
                }}
                onClick={() => setSelected(item.name)}
                actions={[
                  <Popconfirm
                    key="del"
                    title="Delete this skill?"
                    onConfirm={() => handleDelete(item.name)}
                  >
                    <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={e => e.stopPropagation()} />
                  </Popconfirm>
                ]}
              >
                <List.Item.Meta
                  title={<Text strong>{item.name}</Text>}
                  description={<Text type="secondary" ellipsis>{item.description}</Text>}
                />
              </List.Item>
            )}
          />
        )}
      </Card>

      {/* Right panel: file editor */}
      <Card
        style={{ flex: 1, display: 'flex', flexDirection: 'column' }}
        title={selected ? (
          <Space>
            <Text strong>{selected}</Text>
            <Text type="secondary">/</Text>
            <Text>{selectedFile}</Text>
            {dirty && <Tag color="orange">unsaved</Tag>}
          </Space>
        ) : 'Select a skill'}
        extra={selected && (
          <Button type="primary" loading={saving} disabled={!dirty} onClick={handleSave}>
            Save
          </Button>
        )}
        bodyStyle={{ flex: 1, padding: 0, display: 'flex', flexDirection: 'column' }}
      >
        {selected && selectedSkill ? (
          <>
            {/* File tabs */}
            <div style={{ padding: '8px 16px', borderBottom: '1px solid #f0f0f0', display: 'flex', gap: 4, flexWrap: 'wrap' }}>
              {selectedSkill.files.map(f => (
                <Button
                  key={f}
                  size="small"
                  type={selectedFile === f ? 'primary' : 'default'}
                  icon={f.endsWith('.py') ? <CodeOutlined /> : <FileOutlined />}
                  onClick={() => loadFile(selected, f)}
                >
                  {f}
                </Button>
              ))}
            </div>
            {/* Editor */}
            <div style={{ flex: 1 }}>
              <Editor
                language={languageForFile(selectedFile)}
                value={fileContent}
                onChange={(v) => { setFileContent(v || ''); setDirty(true); }}
                options={{ minimap: { enabled: false }, fontSize: 14, wordWrap: 'on' }}
              />
            </div>
          </>
        ) : (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Empty description="Select a skill from the left panel" />
          </div>
        )}
      </Card>

      {/* Create modal */}
      <Modal
        title="New Skill"
        open={createOpen}
        onOk={handleCreate}
        onCancel={() => { setCreateOpen(false); setNewName(''); }}
        okText="Create"
      >
        <Input
          placeholder="Skill name (e.g. my_tool)"
          value={newName}
          onChange={e => setNewName(e.target.value)}
          onPressEnter={handleCreate}
        />
      </Modal>
    </div>
  );
}
```

- [ ] **Step 3: Add route and menu item in App.tsx**

In `apps/monitor-dashboard-web/src/App.tsx`:

Add lazy import (after the other lazy imports):

```typescript
const Skills = lazy(() => import('./pages/Skills'));
```

Add to `menuItems` array (before the second `{ type: 'divider' }`):

```typescript
  { key: '/skills', icon: <ThunderboltOutlined />, label: '技能管理' },
```

Add Route (after the `dynamic-config` route):

```tsx
                  <Route path="/skills" element={<Skills />} />
```

- [ ] **Step 4: Verify frontend builds**

Run: `cd apps/monitor-dashboard-web && npx tsc --noEmit`
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add apps/monitor-dashboard-web/src/pages/Skills.tsx apps/monitor-dashboard-web/src/App.tsx apps/monitor-dashboard-web/package.json apps/monitor-dashboard-web/package-lock.json
git commit -m "feat(dashboard-web): skill management page with Monaco editor"
```

---

## Task 9: Infra — NFS Server + K8s PV/PVC

**Note:** This task requires SSH access to the infra node and kubectl. It's a one-time manual setup.

- [ ] **Step 1: Create NFS export directory on infra node**

SSH to infra node (n37-018-206) and run:

```bash
sudo mkdir -p /data00/k8s-volumes/shared-skills
sudo chmod 777 /data00/k8s-volumes/shared-skills
```

- [ ] **Step 2: Install and configure NFS server**

```bash
sudo apt-get update && sudo apt-get install -y nfs-kernel-server
echo '/data00/k8s-volumes/shared-skills *(rw,sync,no_subtree_check,no_root_squash)' | sudo tee -a /etc/exports
sudo exportfs -ra
sudo systemctl enable nfs-kernel-server
sudo systemctl restart nfs-kernel-server
```

- [ ] **Step 3: Install NFS client on all worker nodes**

On each worker node (n37-078-098, n251-235-105):

```bash
sudo apt-get update && sudo apt-get install -y nfs-common
```

- [ ] **Step 4: Verify NFS mount works from app node**

SSH to app node (n37-078-098):

```bash
sudo mount -t nfs 10.37.18.206:/data00/k8s-volumes/shared-skills /mnt
ls /mnt
sudo umount /mnt
```

- [ ] **Step 5: Copy existing skill definitions to NFS**

On infra node:

```bash
# 从代码仓库复制现有 skill 定义
cp -r /path/to/apps/agent-service/app/skills/definitions/* /data00/k8s-volumes/shared-skills/
ls -la /data00/k8s-volumes/shared-skills/
```

Expected: `bangumi/`, `donjin_search/`, `drawing/`, `hello_sandbox/` directories

- [ ] **Step 6: Create K8s PV and PVC**

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-shared-skills
spec:
  capacity:
    storage: 1Gi
  accessModes:
    - ReadWriteMany
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
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Gi
  volumeName: pv-shared-skills
  storageClassName: ""
EOF
```

- [ ] **Step 7: Verify PVC is bound**

```bash
kubectl get pvc pvc-shared-skills -n prod
```

Expected: STATUS = `Bound`

- [ ] **Step 8: Commit (no code changes, but document the NFS setup)**

No git commit needed — this is infrastructure setup.

---

## Task 10: Deploy and Verify

- [ ] **Step 1: Deploy PaaS Engine**

```bash
make self-deploy GIT_REF=refactor/skill-decouple
```

- [ ] **Step 2: Configure volume mounts on apps**

Using `/api-test` skill:

```bash
# agent-service
.claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/apps/agent-service" \
  '{"volumes":[{"pvc_name":"pvc-shared-skills","mount_path":"/data/skills","read_only":true}]}' \
  "Authorization: Bearer $PAAS_API_TOKEN"

# sandbox-worker
.claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/apps/sandbox-worker" \
  '{"volumes":[{"pvc_name":"pvc-shared-skills","mount_path":"/sandbox/skills","read_only":true}]}' \
  "Authorization: Bearer $PAAS_API_TOKEN"

# monitor-dashboard
.claude/skills/api-test/scripts/http.sh PUT "$PAAS_API/api/paas/apps/monitor-dashboard" \
  '{"volumes":[{"pvc_name":"pvc-shared-skills","mount_path":"/data/skills","read_only":false}]}' \
  "Authorization: Bearer $PAAS_API_TOKEN"
```

- [ ] **Step 3: Deploy agent-service + sync workers**

```bash
make deploy APP=agent-service GIT_REF=refactor/skill-decouple
make release APP=arq-worker LANE=prod VERSION=<新版本> GIT_REF=refactor/skill-decouple
make release APP=vectorize-worker LANE=prod VERSION=<新版本> GIT_REF=refactor/skill-decouple
```

- [ ] **Step 4: Deploy sandbox-worker**

```bash
make deploy APP=sandbox-worker GIT_REF=refactor/skill-decouple
```

- [ ] **Step 5: Deploy dashboard**

```bash
make deploy APP=monitor-dashboard GIT_REF=refactor/skill-decouple
```

- [ ] **Step 6: Verify — Dashboard skill list**

Open Dashboard → Skills page, verify 4 skills are listed with correct names and descriptions.

- [ ] **Step 7: Verify — Edit a skill on Dashboard**

Edit `hello_sandbox` SKILL.md via Dashboard, add a comment line. Wait 30 seconds.

Check agent-service logs:

```bash
make logs APP=agent-service KEYWORD="Skill files changed"
```

Expected: reload log message

- [ ] **Step 8: Verify — Skill execution via Feishu**

Bind dev bot, send a message that triggers skill loading (e.g. ask about anime), verify sandbox_bash works and scripts execute.

- [ ] **Step 9: Cleanup — Remove definitions from repo**

```bash
rm -rf apps/agent-service/app/skills/definitions/
git add -A && git commit -m "refactor: remove skill definitions from repo (moved to NFS)"
```
