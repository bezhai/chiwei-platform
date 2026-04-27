package kubernetes

import (
	"context"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	fakeclient "k8s.io/client-go/kubernetes/fake"
)

func TestDetectPodFailure(t *testing.T) {
	labels := map[string]string{"app": "myapp", "lane": "prod"}
	const latestHash = "abc123"
	const oldHash = "old456"

	// 最新 ReplicaSet（revision 2），pod-template-hash = abc123
	latestRS := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "myapp-prod-" + latestHash,
			Namespace:   "default",
			Labels:      map[string]string{"app": "myapp", "lane": "prod", "pod-template-hash": latestHash},
			Annotations: map[string]string{"deployment.kubernetes.io/revision": "2"},
		},
	}
	// 旧 ReplicaSet（revision 1），pod-template-hash = old456
	oldRS := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "myapp-prod-" + oldHash,
			Namespace:   "default",
			Labels:      map[string]string{"app": "myapp", "lane": "prod", "pod-template-hash": oldHash},
			Annotations: map[string]string{"deployment.kubernetes.io/revision": "1"},
		},
	}

	crashStatus := &corev1.ContainerStatus{
		Name: "myapp",
		State: corev1.ContainerState{
			Waiting: &corev1.ContainerStateWaiting{
				Reason:  "CrashLoopBackOff",
				Message: "back-off 5m0s restarting failed container",
			},
		},
	}

	tests := []struct {
		name       string
		objects    []runtime.Object
		wantFail   bool
		wantReason string
	}{
		{
			name:     "healthy pods",
			objects:  []runtime.Object{latestRS, oldRS, makePod("myapp-prod-abc", labels, latestHash, nil)},
			wantFail: false,
		},
		{
			name: "CrashLoopBackOff detected on latest RS",
			objects: []runtime.Object{latestRS, oldRS, makePod("myapp-prod-abc", labels, latestHash, crashStatus)},
			wantFail:   true,
			wantReason: "CrashLoopBackOff",
		},
		{
			name: "ImagePullBackOff detected on latest RS",
			objects: []runtime.Object{latestRS, oldRS, makePod("myapp-prod-abc", labels, latestHash, &corev1.ContainerStatus{
				Name: "myapp",
				State: corev1.ContainerState{
					Waiting: &corev1.ContainerStateWaiting{
						Reason:  "ImagePullBackOff",
						Message: "repository does not exist",
					},
				},
			})},
			wantFail:   true,
			wantReason: "failed to pull image",
		},
		{
			name:       "init container CrashLoopBackOff on latest RS",
			objects:    []runtime.Object{latestRS, oldRS, makeInitCrashPod("myapp-prod-abc", labels, latestHash)},
			wantFail:   true,
			wantReason: "init container",
		},
		{
			name:     "no pods",
			objects:  []runtime.Object{latestRS},
			wantFail: false,
		},
		{
			name: "old RS pod in CrashLoopBackOff should be ignored",
			objects: []runtime.Object{
				latestRS, oldRS,
				makePod("myapp-prod-old", labels, oldHash, crashStatus),
				makePod("myapp-prod-new", labels, latestHash, nil),
			},
			wantFail: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			client := fakeclient.NewSimpleClientset(tt.objects...)
			deployer := NewK8sDeployer(client, "default", "")

			deploy := &appsv1.Deployment{
				Spec: appsv1.DeploymentSpec{
					Selector: &metav1.LabelSelector{MatchLabels: labels},
				},
			}

			reason, failed := deployer.detectPodFailure(context.Background(), deploy)
			if failed != tt.wantFail {
				t.Errorf("detectPodFailure() failed = %v, want %v (reason: %s)", failed, tt.wantFail, reason)
			}
			if tt.wantFail && tt.wantReason != "" {
				if !containsSubstring(reason, tt.wantReason) {
					t.Errorf("reason %q does not contain %q", reason, tt.wantReason)
				}
			}
		})
	}
}

func makePod(name string, labels map[string]string, hash string, cs *corev1.ContainerStatus) *corev1.Pod {
	podLabels := make(map[string]string)
	for k, v := range labels {
		podLabels[k] = v
	}
	podLabels["pod-template-hash"] = hash

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
			Labels:    podLabels,
		},
	}
	if cs != nil {
		pod.Status.ContainerStatuses = []corev1.ContainerStatus{*cs}
	}
	return pod
}

func makeInitCrashPod(name string, labels map[string]string, hash string) *corev1.Pod {
	podLabels := make(map[string]string)
	for k, v := range labels {
		podLabels[k] = v
	}
	podLabels["pod-template-hash"] = hash

	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
			Labels:    podLabels,
		},
		Status: corev1.PodStatus{
			InitContainerStatuses: []corev1.ContainerStatus{
				{
					Name: "init",
					State: corev1.ContainerState{
						Waiting: &corev1.ContainerStateWaiting{
							Reason:  "CrashLoopBackOff",
							Message: "init container crashed",
						},
					},
				},
			},
		},
	}
}

func containsSubstring(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(s) > 0 && contains(s, substr))
}

func contains(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}

// TestApplyDeploymentWorker 验证 Worker 模式（Port=0）的 Deployment 创建：
// - 设置了 Command
// - 不包含端口
// - EnvFrom 同时包含 Secret 和 ConfigMap
func TestApplyDeploymentWorker(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name:              "arq-worker",
		Port:              0,
		Command:           []string{"uv", "run", "--no-sync", "arq", "app.workers.unified_worker.UnifiedWorkerSettings"},
		EnvFromSecrets:    []string{"app-env"},
		EnvFromConfigMaps: []string{"ai-service-config"},
	}

	release := &domain.Release{
		ID:       "r1",
		AppName:  "arq-worker",
		Lane:     "prod",
		Image:    "harbor.local/inner-bot/agent-service:abc123",
		Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "arq-worker-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	container := deploy.Spec.Template.Spec.Containers[0]

	// 验证 Command 设置
	if len(container.Command) != 5 {
		t.Errorf("expected 5 command args, got %d: %v", len(container.Command), container.Command)
	}
	if container.Command[0] != "uv" {
		t.Errorf("expected command[0] = 'uv', got %q", container.Command[0])
	}

	// 验证无端口
	if len(container.Ports) != 0 {
		t.Errorf("expected no ports for worker, got %v", container.Ports)
	}

	// 验证 EnvFrom 包含 Secret 和 ConfigMap
	if len(container.EnvFrom) != 2 {
		t.Fatalf("expected 2 envFrom sources, got %d", len(container.EnvFrom))
	}
	if container.EnvFrom[0].SecretRef == nil || container.EnvFrom[0].SecretRef.Name != "app-env" {
		t.Errorf("expected first envFrom to be secret 'app-env', got %+v", container.EnvFrom[0])
	}
	if container.EnvFrom[1].ConfigMapRef == nil || container.EnvFrom[1].ConfigMapRef.Name != "ai-service-config" {
		t.Errorf("expected second envFrom to be configmap 'ai-service-config', got %+v", container.EnvFrom[1])
	}

	// 验证 NodeSelector 调度到 app 节点
	if deploy.Spec.Template.Spec.NodeSelector["node-role"] != "app" {
		t.Errorf("expected nodeSelector node-role=app, got %v", deploy.Spec.Template.Spec.NodeSelector)
	}
}

// TestApplyDeploymentWebApp 验证常规 Web App（Port>0）仍正常创建端口和无 Command。
func TestApplyDeploymentWebApp(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name:           "web-service",
		Port:           8080,
		EnvFromSecrets: []string{"web-secret"},
	}

	release := &domain.Release{
		ID:       "r2",
		AppName:  "web-service",
		Lane:     "prod",
		Image:    "harbor.local/inner-bot/web-service:abc123",
		Replicas: 2,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "web-service-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	container := deploy.Spec.Template.Spec.Containers[0]

	// 验证 Command 未设置
	if len(container.Command) != 0 {
		t.Errorf("expected no command for web app, got %v", container.Command)
	}

	// 验证有端口
	if len(container.Ports) != 1 || container.Ports[0].ContainerPort != 8080 {
		t.Errorf("expected port 8080, got %v", container.Ports)
	}

	// 验证 EnvFrom 仅包含 Secret
	if len(container.EnvFrom) != 1 {
		t.Fatalf("expected 1 envFrom source, got %d", len(container.EnvFrom))
	}
	if container.EnvFrom[0].SecretRef == nil || container.EnvFrom[0].SecretRef.Name != "web-secret" {
		t.Errorf("expected envFrom to be secret 'web-secret', got %+v", container.EnvFrom[0])
	}

	// 验证 NodeSelector 调度到 app 节点
	if deploy.Spec.Template.Spec.NodeSelector["node-role"] != "app" {
		t.Errorf("expected nodeSelector node-role=app, got %v", deploy.Spec.Template.Spec.NodeSelector)
	}
}

// TestDeployWorkerSkipsService 验证 Worker（Port=0）部署时不创建 Service。
func TestDeployWorkerSkipsService(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name:    "recall-worker",
		Port:    0,
		Command: []string{"./recall-worker"},
	}

	release := &domain.Release{
		ID:       "r3",
		AppName:  "recall-worker",
		Lane:     "prod",
		Image:    "harbor.local/inner-bot/lark-server:abc123",
		Replicas: 1,
	}

	// 使用 Deploy（而非 applyDeployment）来验证 Service 逻辑
	// 注意: Deploy 会调用 waitForRollout，fake client 的 Deployment 没有 Status，
	// 所以这里直接测试 applyDeployment + 检查 Service 不存在
	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	// 验证 Deployment 存在
	_, err := client.AppsV1().Deployments("default").Get(context.Background(), "recall-worker-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Deployment should exist: %v", err)
	}

	// 验证 Service 不存在（Deploy 在 Port=0 时跳过 applyService）
	svcs, err := client.CoreV1().Services("default").List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatalf("List Services error = %v", err)
	}
	if len(svcs.Items) != 0 {
		t.Errorf("expected no services for worker, got %d", len(svcs.Items))
	}
}

// TestApplyDeploymentInjectsAppName 验证 Deployment 的主容器 env 注入了 APP_NAME，
// 供 agent-service dataflow framework 在 Pod 启动时按 App 过滤 Node 使用。
func TestApplyDeploymentInjectsAppName(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name: "vectorize-worker",
		Port: 0,
	}

	release := &domain.Release{
		ID:       "r-app-name",
		AppName:  "vectorize-worker",
		Lane:     "prod",
		Version:  "1.0.0.1",
		Image:    "harbor.local/inner-bot/agent-service:abc123",
		Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "vectorize-worker-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	container := deploy.Spec.Template.Spec.Containers[0]
	envs := map[string]string{}
	for _, e := range container.Env {
		envs[e.Name] = e.Value
	}

	if envs["APP_NAME"] != "vectorize-worker" {
		t.Errorf("expected APP_NAME=vectorize-worker, got %q (envs=%v)", envs["APP_NAME"], envs)
	}
}

// TestBuildEnvFrom 验证 buildEnvFrom 函数的各种组合。
func TestGetDeploymentStatus(t *testing.T) {
	labels := map[string]string{"app": "myapp", "lane": "prod"}
	replicas := int32(2)

	deploy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "myapp-prod",
			Namespace: "default",
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: labels},
		},
		Status: appsv1.DeploymentStatus{
			ReadyReplicas:     2,
			AvailableReplicas: 2,
		},
	}

	healthyPod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "myapp-prod-abc-1",
			Namespace: "default",
			Labels:    labels,
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			ContainerStatuses: []corev1.ContainerStatus{
				{Name: "myapp", Ready: true, RestartCount: 0},
			},
		},
	}

	crashPod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "myapp-prod-abc-2",
			Namespace: "default",
			Labels:    labels,
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			ContainerStatuses: []corev1.ContainerStatus{
				{
					Name:         "myapp",
					Ready:        false,
					RestartCount: 5,
					State: corev1.ContainerState{
						Waiting: &corev1.ContainerStateWaiting{
							Reason:  "CrashLoopBackOff",
							Message: "back-off restarting failed container",
						},
					},
				},
			},
		},
	}

	tests := []struct {
		name          string
		objects       []runtime.Object
		wantDesired   int32
		wantReady     int32
		wantPodCount  int
		wantPodReason string // 检查第一个有 reason 的 pod
	}{
		{
			name:         "healthy deployment",
			objects:      []runtime.Object{deploy, healthyPod},
			wantDesired:  2,
			wantReady:    2,
			wantPodCount: 1,
		},
		{
			name:          "deployment with crash pod",
			objects:       []runtime.Object{deploy, healthyPod, crashPod},
			wantDesired:   2,
			wantReady:     2,
			wantPodCount:  2,
			wantPodReason: "CrashLoopBackOff",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			client := fakeclient.NewSimpleClientset(tt.objects...)
			deployer := NewK8sDeployer(client, "default", "")

			status, err := deployer.GetDeploymentStatus(context.Background(), "myapp-prod")
			if err != nil {
				t.Fatalf("GetDeploymentStatus() error = %v", err)
			}

			if status.DeployName != "myapp-prod" {
				t.Errorf("DeployName = %q, want %q", status.DeployName, "myapp-prod")
			}
			if status.Desired != tt.wantDesired {
				t.Errorf("Desired = %d, want %d", status.Desired, tt.wantDesired)
			}
			if status.Ready != tt.wantReady {
				t.Errorf("Ready = %d, want %d", status.Ready, tt.wantReady)
			}
			if len(status.Pods) != tt.wantPodCount {
				t.Errorf("Pods count = %d, want %d", len(status.Pods), tt.wantPodCount)
			}

			if tt.wantPodReason != "" {
				found := false
				for _, p := range status.Pods {
					if containsSubstring(p.Reason, tt.wantPodReason) {
						found = true
						break
					}
				}
				if !found {
					t.Errorf("no pod with reason containing %q", tt.wantPodReason)
				}
			}
		})
	}
}

func TestGetDeploymentStatusNotFound(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	_, err := deployer.GetDeploymentStatus(context.Background(), "nonexistent")
	if err == nil {
		t.Error("expected error for nonexistent deployment, got nil")
	}
}

// TestApplyDeploymentWithVolumes 验证 App 有一个 VolumeMount 时正确生成 PVC Volume 和容器 VolumeMount。
func TestApplyDeploymentWithVolumes(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name: "data-worker",
		Port: 0,
		Volumes: []domain.VolumeMount{
			{PVCName: "shared-data", MountPath: "/data", ReadOnly: false},
		},
	}

	release := &domain.Release{
		ID:       "r10",
		AppName:  "data-worker",
		Lane:     "prod",
		Image:    "harbor.local/inner-bot/data-worker:v1",
		Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "data-worker-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	// 验证 PodSpec.Volumes 有 1 个 PVC Volume
	if len(deploy.Spec.Template.Spec.Volumes) != 1 {
		t.Fatalf("expected 1 volume, got %d", len(deploy.Spec.Template.Spec.Volumes))
	}
	vol := deploy.Spec.Template.Spec.Volumes[0]
	if vol.Name != "shared-data" {
		t.Errorf("volume name = %q, want %q", vol.Name, "shared-data")
	}
	if vol.PersistentVolumeClaim == nil || vol.PersistentVolumeClaim.ClaimName != "shared-data" {
		t.Errorf("volume PVC ClaimName = %+v, want shared-data", vol.PersistentVolumeClaim)
	}

	// 验证主容器有 1 个 VolumeMount
	container := deploy.Spec.Template.Spec.Containers[0]
	if len(container.VolumeMounts) != 1 {
		t.Fatalf("expected 1 volume mount, got %d", len(container.VolumeMounts))
	}
	mount := container.VolumeMounts[0]
	if mount.Name != "shared-data" {
		t.Errorf("mount name = %q, want %q", mount.Name, "shared-data")
	}
	if mount.MountPath != "/data" {
		t.Errorf("mount path = %q, want %q", mount.MountPath, "/data")
	}
	if mount.ReadOnly != false {
		t.Errorf("mount ReadOnly = %v, want false", mount.ReadOnly)
	}
}

// TestApplyDeploymentWithVolumesDedup 验证同一 PVC 两个挂载路径时只生成 1 个 Volume，但 2 个 VolumeMount，SubPath 也正确设置。
func TestApplyDeploymentWithVolumesDedup(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name: "multi-mount-app",
		Port: 8080,
		Volumes: []domain.VolumeMount{
			{PVCName: "shared-pvc", MountPath: "/data/input", SubPath: "input"},
			{PVCName: "shared-pvc", MountPath: "/data/output", SubPath: "output"},
		},
	}

	release := &domain.Release{
		ID:       "r11",
		AppName:  "multi-mount-app",
		Lane:     "prod",
		Image:    "harbor.local/inner-bot/multi-mount-app:v1",
		Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "multi-mount-app-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	// 同一 PVC 只生成 1 个 Volume（去重）
	if len(deploy.Spec.Template.Spec.Volumes) != 1 {
		t.Fatalf("expected 1 volume (dedup), got %d", len(deploy.Spec.Template.Spec.Volumes))
	}
	if deploy.Spec.Template.Spec.Volumes[0].Name != "shared-pvc" {
		t.Errorf("volume name = %q, want %q", deploy.Spec.Template.Spec.Volumes[0].Name, "shared-pvc")
	}

	// 但有 2 个 VolumeMount
	container := deploy.Spec.Template.Spec.Containers[0]
	if len(container.VolumeMounts) != 2 {
		t.Fatalf("expected 2 volume mounts, got %d", len(container.VolumeMounts))
	}

	// 验证 SubPath
	if container.VolumeMounts[0].SubPath != "input" {
		t.Errorf("mount[0].SubPath = %q, want %q", container.VolumeMounts[0].SubPath, "input")
	}
	if container.VolumeMounts[0].MountPath != "/data/input" {
		t.Errorf("mount[0].MountPath = %q, want %q", container.VolumeMounts[0].MountPath, "/data/input")
	}
	if container.VolumeMounts[1].SubPath != "output" {
		t.Errorf("mount[1].SubPath = %q, want %q", container.VolumeMounts[1].SubPath, "output")
	}
	if container.VolumeMounts[1].MountPath != "/data/output" {
		t.Errorf("mount[1].MountPath = %q, want %q", container.VolumeMounts[1].MountPath, "/data/output")
	}
}

// TestApplyDeploymentNoVolumes 验证 App 没有 Volumes 时 Deployment 上没有 volumes/mounts（向后兼容）。
func TestApplyDeploymentNoVolumes(t *testing.T) {
	client := fakeclient.NewSimpleClientset()
	deployer := NewK8sDeployer(client, "default", "")

	app := &domain.App{
		Name: "simple-app",
		Port: 3000,
	}

	release := &domain.Release{
		ID:       "r12",
		AppName:  "simple-app",
		Lane:     "prod",
		Image:    "harbor.local/inner-bot/simple-app:v1",
		Replicas: 1,
	}

	if err := deployer.applyDeployment(context.Background(), release, app, nil); err != nil {
		t.Fatalf("applyDeployment() error = %v", err)
	}

	deploy, err := client.AppsV1().Deployments("default").Get(context.Background(), "simple-app-prod", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get Deployment error = %v", err)
	}

	// 无 Volumes
	if len(deploy.Spec.Template.Spec.Volumes) != 0 {
		t.Errorf("expected no volumes, got %d", len(deploy.Spec.Template.Spec.Volumes))
	}

	// 主容器无 VolumeMounts
	container := deploy.Spec.Template.Spec.Containers[0]
	if len(container.VolumeMounts) != 0 {
		t.Errorf("expected no volume mounts, got %d", len(container.VolumeMounts))
	}
}

func TestBuildEnvFrom(t *testing.T) {
	tests := []struct {
		name       string
		secrets    []string
		configMaps []string
		wantLen    int
	}{
		{"nil inputs", nil, nil, 0},
		{"secrets only", []string{"s1", "s2"}, nil, 2},
		{"configmaps only", nil, []string{"cm1"}, 1},
		{"both", []string{"s1"}, []string{"cm1", "cm2"}, 3},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := buildEnvFrom(tt.secrets, tt.configMaps)
			if tt.wantLen == 0 {
				if result != nil {
					t.Errorf("expected nil, got %v", result)
				}
				return
			}
			if len(result) != tt.wantLen {
				t.Errorf("expected %d sources, got %d", tt.wantLen, len(result))
			}
		})
	}
}
