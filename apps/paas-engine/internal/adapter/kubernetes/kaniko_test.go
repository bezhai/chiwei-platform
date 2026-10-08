package kubernetes

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"log/slog"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

func TestSubmit_ContextDirArgs(t *testing.T) {
	tests := []struct {
		name       string
		contextDir string
		wantArg    string // 期望包含的参数
		wantAbsent string // 期望不包含的参数前缀
	}{
		{
			name:       "子目录构建：使用 --context-sub-path",
			contextDir: "apps/channel-server",
			wantArg:    "--context-sub-path=apps/channel-server",
		},
		{
			name:       "空 context_dir：不追加子路径",
			contextDir: "",
			wantAbsent: "--context-sub-path=",
		},
		{
			name:       "根目录构建(.)：不追加子路径",
			contextDir: ".",
			wantAbsent: "--context-sub-path=",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			client := fake.NewSimpleClientset()
			executor := NewKanikoBuildExecutor(client, KanikoBuildConfig{
				Namespace:   "paas-builds",
				KanikoImage: "gcr.io/kaniko-project/executor:latest",
			})

			sub := &port.BuildSubmission{
				BuildID:    "test-build-id",
				GitRepo:    "https://github.com/example/repo",
				GitRef:     "main",
				ImageTag:   "registry.example.com/app:latest",
				ContextDir: tt.contextDir,
			}

			_, err := executor.Submit(context.Background(), sub)
			if err != nil {
				t.Fatalf("Submit() error = %v", err)
			}

			jobs, err := client.BatchV1().Jobs("paas-builds").List(context.Background(), metav1.ListOptions{})
			if err != nil {
				t.Fatalf("List jobs error = %v", err)
			}
			if len(jobs.Items) != 1 {
				t.Fatalf("expected 1 job, got %d", len(jobs.Items))
			}

			args := jobs.Items[0].Spec.Template.Spec.Containers[0].Args

			if tt.wantArg != "" {
				if !containsArg(args, tt.wantArg) {
					t.Errorf("expected args to contain %q, got %v", tt.wantArg, args)
				}
			}

			if tt.wantAbsent != "" {
				if containsArgPrefix(args, tt.wantAbsent) {
					t.Errorf("expected args NOT to contain prefix %q, got %v", tt.wantAbsent, args)
				}
			}
		})
	}
}

func TestJobToStatus(t *testing.T) {
	tests := []struct {
		name       string
		job        *batchv1.Job
		wantStatus domain.BuildStatus
	}{
		{
			name: "job succeeded",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobComplete, Status: "True"},
					},
				},
			},
			wantStatus: domain.BuildStatusSucceeded,
		},
		{
			name: "job failed",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobFailed, Status: "True"},
					},
				},
			},
			wantStatus: domain.BuildStatusFailed,
		},
		{
			name: "job running",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Active: 1,
				},
			},
			wantStatus: domain.BuildStatusRunning,
		},
		{
			name:       "job pending (no conditions, no active)",
			job:        &batchv1.Job{},
			wantStatus: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			status, _ := jobToStatus(tt.job)
			if status != tt.wantStatus {
				t.Errorf("jobToStatus() = %v, want %v", status, tt.wantStatus)
			}
		})
	}
}

// 构建 Pod 必须落在 app 节点上：只有它有能拉 git 仓库和 base 镜像的外网出口。
// proxy1（node-role=proxy）虽然直连外网，但到不了公司代理，落上去构建必失败。
func TestSubmit_PinsBuildToAppNode(t *testing.T) {
	client := fake.NewSimpleClientset()
	executor := NewKanikoBuildExecutor(client, KanikoBuildConfig{
		Namespace:   "paas-builds",
		KanikoImage: "gcr.io/kaniko-project/executor:latest",
	})

	_, err := executor.Submit(context.Background(), &port.BuildSubmission{
		BuildID:  "test-build-id",
		GitRepo:  "https://github.com/example/repo",
		GitRef:   "main",
		ImageTag: "registry.example.com/app:latest",
	})
	if err != nil {
		t.Fatalf("Submit() error = %v", err)
	}

	jobs, err := client.BatchV1().Jobs("paas-builds").List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatalf("List jobs error = %v", err)
	}
	if len(jobs.Items) != 1 {
		t.Fatalf("expected 1 job, got %d", len(jobs.Items))
	}

	got := jobs.Items[0].Spec.Template.Spec.NodeSelector
	if got["node-role"] != "app" {
		t.Errorf("NodeSelector = %v, want node-role=app", got)
	}
}

// 构建必须有执行期限。没有的话，Job 排不上（app 节点被 cordon / label 改了 / 资源不够）
// 就永远停在 Pending，而 build 记录在 Submit 成功那一刻就写成了 running
// （build_service.go:120），Makefile 的轮询只认 succeeded/failed/cancelled，
// 于是表现成「构建永远 running、日志为空」。activeDeadlineSeconds 从 Job 创建时刻起算、
// 不管 Pod 有没有跑起来，所以排不上的 Job 也会到点变 Failed，轮询才有终点。
func TestSubmit_HasExecutionDeadline(t *testing.T) {
	client := fake.NewSimpleClientset()
	executor := NewKanikoBuildExecutor(client, KanikoBuildConfig{
		Namespace:   "paas-builds",
		KanikoImage: "gcr.io/kaniko-project/executor:latest",
	})

	_, err := executor.Submit(context.Background(), &port.BuildSubmission{
		BuildID:  "test-build-id",
		GitRepo:  "https://github.com/example/repo",
		GitRef:   "main",
		ImageTag: "registry.example.com/app:latest",
	})
	if err != nil {
		t.Fatalf("Submit() error = %v", err)
	}

	jobs, err := client.BatchV1().Jobs("paas-builds").List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatalf("List jobs error = %v", err)
	}
	if len(jobs.Items) != 1 {
		t.Fatalf("expected 1 job, got %d", len(jobs.Items))
	}

	got := jobs.Items[0].Spec.ActiveDeadlineSeconds
	if got == nil {
		t.Fatal("ActiveDeadlineSeconds is nil: 排不上的构建会永远停在 running")
	}
	// 观测到的最慢一次真实构建 425s，留 4 倍余量
	if *got != 1800 {
		t.Errorf("ActiveDeadlineSeconds = %d, want 1800", *got)
	}
}

const (
	testBuildNS      = "paas-builds"
	testGitToken     = "ghp_unit_test_token_7f3a"
	testSecretPrefix = "kaniko-git-auth"
)

func newGitAuthExecutor(client *fake.Clientset, token, lane string) *KanikoBuildExecutor {
	return NewKanikoBuildExecutor(client, KanikoBuildConfig{
		Namespace:           testBuildNS,
		KanikoImage:         "gcr.io/kaniko-project/executor:latest",
		RegistrySecret:      "harbor-secret",
		GitToken:            token,
		GitAuthSecretPrefix: testSecretPrefix,
		Lane:                lane,
	})
}

// submitBuild 提交一次构建并取回创建出来的 Job。构建本身必须提交成功：
// git 凭据同步失败只影响这次怎么克隆，不能让构建提交失败。
func submitBuild(t *testing.T, client *fake.Clientset, executor *KanikoBuildExecutor, buildID string) *batchv1.Job {
	t.Helper()
	jobName, err := executor.Submit(context.Background(), &port.BuildSubmission{
		BuildID:  buildID,
		GitRepo:  "bezhai/chiwei-platform",
		GitRef:   "main",
		ImageTag: "registry.example.com/app:latest",
	})
	if err != nil {
		t.Fatalf("Submit() error = %v", err)
	}
	job, err := client.BatchV1().Jobs(testBuildNS).Get(context.Background(), jobName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get job %s: %v", jobName, err)
	}
	return job
}

// gitEnv 取出 kaniko 容器里 GIT_ 开头的环境变量：kaniko 只从这几个变量拿 git 凭据。
func gitEnv(job *batchv1.Job) map[string]corev1.EnvVar {
	out := map[string]corev1.EnvVar{}
	for _, e := range job.Spec.Template.Spec.Containers[0].Env {
		if strings.HasPrefix(e.Name, "GIT_") {
			out[e.Name] = e
		}
	}
	return out
}

// assertAnonymousClone 断言 Job 不带任何 git 凭据，即今天的匿名克隆。
func assertAnonymousClone(t *testing.T, job *batchv1.Job) {
	t.Helper()
	if env := gitEnv(job); len(env) != 0 {
		t.Errorf("Job 不应带 git 凭据，却有 %v", env)
	}
	if ef := job.Spec.Template.Spec.Containers[0].EnvFrom; len(ef) != 0 {
		t.Errorf("Job 不应有 envFrom，却有 %v", ef)
	}
}

// assertReferencesGitSecret 断言 Job 从指定 Secret 读 GIT_USERNAME / GIT_PASSWORD，且不设 GIT_TOKEN。
func assertReferencesGitSecret(t *testing.T, job *batchv1.Job, secretName string) {
	t.Helper()
	env := gitEnv(job)
	// kaniko v1.24.0 的 GIT_TOKEN 会被当成用户名、密码为空，还会盖掉另外两个变量
	if _, ok := env["GIT_TOKEN"]; ok {
		t.Errorf("Job 不应设置 GIT_TOKEN")
	}
	for _, key := range []string{"GIT_USERNAME", "GIT_PASSWORD"} {
		e, ok := env[key]
		if !ok {
			t.Errorf("Job 缺少 %s", key)
			continue
		}
		if e.Value != "" {
			t.Errorf("%s 不应是明文值", key)
		}
		if e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil {
			t.Errorf("%s 应引用 Secret，实际 %+v", key, e)
			continue
		}
		ref := e.ValueFrom.SecretKeyRef
		if ref.Name != secretName || ref.Key != key {
			t.Errorf("%s 引用 %s/%s，期望 %s/%s", key, ref.Name, ref.Key, secretName, key)
		}
		// Pod 启动前 Secret 若被删，退回匿名克隆，而不是卡在 CreateContainerConfigError 等到 deadline
		if ref.Optional == nil || !*ref.Optional {
			t.Errorf("%s 的 Secret 引用应为 optional", key)
		}
	}
}

// secretValues 读 Secret 的生效内容。真实 API server 写入时把 StringData 合并进 Data
// （同名键以 StringData 为准），fake clientset 不做这一步，这里按同样的规则合并。
func secretValues(s *corev1.Secret) map[string]string {
	out := map[string]string{}
	for k, v := range s.Data {
		out[k] = string(v)
	}
	for k, v := range s.StringData {
		out[k] = v
	}
	return out
}

func getSecret(t *testing.T, client *fake.Clientset, name string) *corev1.Secret {
	t.Helper()
	s, err := client.CoreV1().Secrets(testBuildNS).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get secret %s: %v", name, err)
	}
	return s
}

// secretVerbs 列出对 secrets 发起过的 API 调用。
func secretVerbs(client *fake.Clientset) []string {
	var verbs []string
	for _, a := range client.Actions() {
		if a.GetResource().Resource == "secrets" {
			verbs = append(verbs, a.GetVerb())
		}
	}
	return verbs
}

// captureLogs 把 slog 默认输出接到一个 buffer 上，测试结束恢复。
func captureLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	prev, prevOut, prevFlags := slog.Default(), log.Writer(), log.Flags()
	buf := &bytes.Buffer{}
	slog.SetDefault(slog.New(slog.NewTextHandler(buf, nil)))
	t.Cleanup(func() {
		slog.SetDefault(prev)
		log.SetOutput(prevOut)
		log.SetFlags(prevFlags)
	})
	return buf
}

func existingGitSecret(name, token string) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: testBuildNS,
			Labels:    map[string]string{"managed-by": "paas-engine"},
		},
		Data: map[string][]byte{
			"GIT_USERNAME": []byte("x-access-token"),
			"GIT_PASSWORD": []byte(token),
		},
	}
}

// 有 token 时：本实例的 Secret 被建出来、只含用户名和 token 两个键，Job 从它读凭据。
// 匿名克隆走共享代理出口 IP 的匿名额度，被限流时 GitHub 对公开仓库也回 401。
func TestSubmit_WithTokenSyncsSecretAndReferencesIt(t *testing.T) {
	client := fake.NewSimpleClientset()
	job := submitBuild(t, client, newGitAuthExecutor(client, testGitToken, "ppe-auth"), "build-1")

	secret := getSecret(t, client, "kaniko-git-auth-ppe-auth")
	if secret.Labels["managed-by"] != "paas-engine" {
		t.Errorf("Secret labels = %v, want managed-by=paas-engine", secret.Labels)
	}
	want := map[string]string{"GIT_USERNAME": "x-access-token", "GIT_PASSWORD": testGitToken}
	if got := secretValues(secret); !reflect.DeepEqual(got, want) {
		t.Errorf("Secret 内容不对（只列键名）: got keys %v", keys(got))
	}
	assertReferencesGitSecret(t, job, "kaniko-git-auth-ppe-auth")
}

// token 只经 Secret 引用进入容器：不能出现在 Job 的参数、明文 env 或任何字段里，
// 否则 kubectl describe pod 就能看到。
func TestSubmit_TokenNeverAppearsInJobSpec(t *testing.T) {
	client := fake.NewSimpleClientset()
	job := submitBuild(t, client, newGitAuthExecutor(client, testGitToken, "prod"), "build-1")

	raw, err := json.Marshal(job)
	if err != nil {
		t.Fatalf("marshal job: %v", err)
	}
	if strings.Contains(string(raw), testGitToken) {
		t.Errorf("token 出现在 Job spec 里")
	}
}

// 轮换 token 后重新部署的实例，下一次构建就把新 token 写进自己的 Secret。
func TestSubmit_TokenChangeUpdatesSecret(t *testing.T) {
	client := fake.NewSimpleClientset(existingGitSecret("kaniko-git-auth-prod", "ghp_old_revoked"))
	job := submitBuild(t, client, newGitAuthExecutor(client, testGitToken, "prod"), "build-1")

	if got := secretValues(getSecret(t, client, "kaniko-git-auth-prod"))["GIT_PASSWORD"]; got != testGitToken {
		t.Errorf("Secret 没有更新成新 token")
	}
	assertReferencesGitSecret(t, job, "kaniko-git-auth-prod")
}

// 本次同步失败时 Job 不引用凭据、按匿名克隆，构建照常提交，并打一条点名 Secret 的告警。
// 旧 Secret 还在而更新失败时如果照样引用，Job 会带着旧 token 去克隆，
// 旧 token 已撤销的话所有构建都 401 —— 比匿名还糟。optional 只管 Secret 不存在，挡不住这种情况。
func TestSubmit_SecretSyncFailureClonesAnonymously(t *testing.T) {
	secrets := schema.GroupResource{Resource: "secrets"}
	tests := []struct {
		name     string
		existing bool
		verb     string
		err      error
		cause    string // 错误原文里不带引号的部分：日志里的错误串会被转义
	}{
		{
			// 同一实例两次提交并发 Get→Update，后到的那次带着过期的 resourceVersion
			name:     "旧 Secret 存在、更新冲突",
			existing: true,
			verb:     "update",
			err:      apierrors.NewConflict(secrets, "kaniko-git-auth-prod", fmt.Errorf("the object has been modified; please apply your changes to the latest version and try again")),
			cause:    "the object has been modified",
		},
		{
			name:     "旧 Secret 存在、无权更新",
			existing: true,
			verb:     "update",
			err:      apierrors.NewForbidden(secrets, "kaniko-git-auth-prod", fmt.Errorf("RBAC: update not allowed")),
			cause:    "RBAC: update not allowed",
		},
		{
			name:  "Secret 不存在、无权创建",
			verb:  "create",
			err:   apierrors.NewForbidden(secrets, "kaniko-git-auth-prod", fmt.Errorf("RBAC: create not allowed")),
			cause: "RBAC: create not allowed",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var client *fake.Clientset
			if tt.existing {
				client = fake.NewSimpleClientset(existingGitSecret("kaniko-git-auth-prod", "ghp_old_revoked"))
			} else {
				client = fake.NewSimpleClientset()
			}
			client.PrependReactor(tt.verb, "secrets", func(k8stesting.Action) (bool, runtime.Object, error) {
				return true, nil, tt.err
			})
			logs := captureLogs(t)

			job := submitBuild(t, client, newGitAuthExecutor(client, testGitToken, "prod"), "build-1")

			assertAnonymousClone(t, job)
			out := logs.String()
			if !strings.Contains(out, "secret=kaniko-git-auth-prod") || !strings.Contains(out, tt.cause) {
				t.Errorf("告警日志应点名 Secret 和错误，实际: %s", out)
			}
			if strings.Contains(out, testGitToken) {
				t.Errorf("日志里不能出现 token")
			}
		})
	}
}

// 同一实例的两次提交，第一次更新成功、第二次撞上 Conflict：只有第二次退回匿名克隆，
// 第一次的 Job 照常引用 Secret。是否带凭据按每次提交各自的同步结果定，不是实例级的状态。
func TestSubmit_ConflictOnlyDropsCredentialsForThatBuild(t *testing.T) {
	client := fake.NewSimpleClientset(existingGitSecret("kaniko-git-auth-prod", "ghp_old_revoked"))
	updates := 0
	client.PrependReactor("update", "secrets", func(k8stesting.Action) (bool, runtime.Object, error) {
		updates++
		if updates == 1 {
			return false, nil, nil // 交给 fake 的默认存储，正常写入
		}
		return true, nil, apierrors.NewConflict(schema.GroupResource{Resource: "secrets"}, "kaniko-git-auth-prod",
			fmt.Errorf("the object has been modified; please apply your changes to the latest version and try again"))
	})
	logs := captureLogs(t)
	executor := newGitAuthExecutor(client, testGitToken, "prod")

	first := submitBuild(t, client, executor, "build-1")
	second := submitBuild(t, client, executor, "build-2")

	assertReferencesGitSecret(t, first, "kaniko-git-auth-prod")
	assertAnonymousClone(t, second)
	if out := logs.String(); !strings.Contains(out, "secret=kaniko-git-auth-prod") || !strings.Contains(out, "the object has been modified") {
		t.Errorf("第二次提交应打出点名 Secret 的冲突告警，实际: %s", out)
	}
}

// 没有 token（从 App env 删掉了）或不知道自己在哪条泳道时：Job 不带凭据，也不碰任何 Secret。
// 删掉 token 就该真的不再用它；LANE 为空时退回一个不带泳道的公共名字，会重新引入实例之间互相覆盖。
func TestSubmit_NoCredentialsLeavesSecretsAlone(t *testing.T) {
	tests := []struct {
		name  string
		token string
		lane  string
	}{
		{name: "未配置 token", token: "", lane: "prod"},
		{name: "LANE 为空", token: testGitToken, lane: ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			residual := existingGitSecret("kaniko-git-auth-prod", "ghp_residual")
			client := fake.NewSimpleClientset(residual)

			job := submitBuild(t, client, newGitAuthExecutor(client, tt.token, tt.lane), "build-1")

			assertAnonymousClone(t, job)
			if verbs := secretVerbs(client); len(verbs) != 0 {
				t.Errorf("不应调用 secrets API，实际调用了 %v", verbs)
			}
			if got := secretValues(getSecret(t, client, "kaniko-git-auth-prod"))["GIT_PASSWORD"]; got != "ghp_residual" {
				t.Errorf("残留 Secret 被改动了")
			}
		})
	}
}

// 每个泳道的 paas-engine 写、读各自的 Secret。共用一个的话，轮换 token 后没重新部署的实例
// 会把旧 token 写回去，覆盖别的实例刚写的新 token，甚至在别人的 Job 提交后、Pod 启动前改掉它要读的值。
func TestSubmit_EachLaneUsesItsOwnSecret(t *testing.T) {
	client := fake.NewSimpleClientset()
	prodJob := submitBuild(t, client, newGitAuthExecutor(client, "ghp_prod_token", "prod"), "build-prod")
	blueJob := submitBuild(t, client, newGitAuthExecutor(client, "ghp_blue_token", "blue"), "build-blue")

	if got := secretValues(getSecret(t, client, "kaniko-git-auth-prod"))["GIT_PASSWORD"]; got != "ghp_prod_token" {
		t.Errorf("prod 的 Secret 被别的实例覆盖了")
	}
	if got := secretValues(getSecret(t, client, "kaniko-git-auth-blue"))["GIT_PASSWORD"]; got != "ghp_blue_token" {
		t.Errorf("blue 的 Secret 内容不对")
	}
	assertReferencesGitSecret(t, prodJob, "kaniko-git-auth-prod")
	assertReferencesGitSecret(t, blueJob, "kaniko-git-auth-blue")
}

func keys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func containsArg(args []string, target string) bool {
	for _, a := range args {
		if a == target {
			return true
		}
	}
	return false
}

func containsArgPrefix(args []string, prefix string) bool {
	for _, a := range args {
		if len(a) >= len(prefix) && a[:len(prefix)] == prefix {
			return true
		}
	}
	return false
}
