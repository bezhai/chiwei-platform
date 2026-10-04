package kubernetes

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"strings"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"
)

var _ port.BuildExecutor = (*KanikoBuildExecutor)(nil)

const labelBuildID = "paas.chiwei/build-id"

// kaniko v1.24.0 用 GIT_USERNAME / GIT_PASSWORD 做 basic auth；GitHub 对 PAT 的标准用法是
// 用户名 x-access-token、密码填 token。不要用 GIT_TOKEN：它会被当成用户名、密码为空，还会盖掉这两个变量。
// Secret 的键名和容器里的变量名一致。
const (
	gitUsernameKey = "GIT_USERNAME"
	gitPasswordKey = "GIT_PASSWORD"
	gitUsername    = "x-access-token"
)

type KanikoBuildExecutor struct {
	client             kubernetes.Interface
	namespace          string
	kanikoImage        string
	registrySecret     string
	registryMirrors    []string
	insecureRegistries []string
	cacheRepo          string
	httpProxy          string
	noProxy            string
	gitToken           string
	gitAuthSecret      string // 空串表示本实例的构建不带 git 凭据
}

type KanikoBuildConfig struct {
	Namespace          string
	KanikoImage        string
	RegistrySecret     string
	RegistryMirrors    []string
	InsecureRegistries []string
	CacheRepo          string
	HttpProxy          string
	NoProxy            string

	GitToken            string // 克隆用的 GitHub token，空则匿名克隆
	GitAuthSecretPrefix string
	Lane                string // 本实例所在泳道，决定它写哪个 Secret
}

func NewKanikoBuildExecutor(client kubernetes.Interface, cfg KanikoBuildConfig) *KanikoBuildExecutor {
	if cfg.GitToken != "" && cfg.Lane == "" {
		slog.Warn("GITHUB_TOKEN is set but LANE is empty, kaniko builds will clone anonymously")
	}
	return &KanikoBuildExecutor{
		client:             client,
		namespace:          cfg.Namespace,
		kanikoImage:        cfg.KanikoImage,
		registrySecret:     cfg.RegistrySecret,
		registryMirrors:    cfg.RegistryMirrors,
		insecureRegistries: cfg.InsecureRegistries,
		cacheRepo:          cfg.CacheRepo,
		httpProxy:          cfg.HttpProxy,
		noProxy:            cfg.NoProxy,
		gitToken:           cfg.GitToken,
		gitAuthSecret:      gitAuthSecretName(cfg),
	}
}

// gitAuthSecretName 返回本实例维护的 git 凭据 Secret 名，空串表示本实例的构建不带凭据。
// 名字带上实例所在泳道：prod / blue / ppe-* 的 paas-engine 都会提交构建，token 只在启动时读一次，
// 共用一个 Secret 的话，轮换后没重新部署的实例会把旧 token 写回去，覆盖别的实例刚写的新 token。
// LANE 为空时不带凭据，而不是退回一个不带泳道的公共名字 —— 那又成了共用。
func gitAuthSecretName(cfg KanikoBuildConfig) string {
	if cfg.GitToken == "" || cfg.Lane == "" {
		return ""
	}
	return cfg.GitAuthSecretPrefix + "-" + cfg.Lane
}

// syncGitAuthSecret 把本实例的 token 写进它自己的 Secret，返回本次 Job 该引用的 Secret 名；
// 返回空串时 Job 不带凭据，按匿名克隆。每次提交都写一遍，Secret 被误删或被改，下一次构建就自愈。
// 是否引用只看这一次写没写成功：旧 Secret 还在而这次更新失败时，引用它就会带着可能已撤销的旧 token 去克隆，
// Secret 引用上的 optional 只管 Secret 不存在，挡不住这种情况。
func (e *KanikoBuildExecutor) syncGitAuthSecret(ctx context.Context) string {
	if e.gitAuthSecret == "" {
		return ""
	}
	err := applySecret(ctx, e.client, e.namespace, e.gitAuthSecret, map[string]string{
		gitUsernameKey: gitUsername,
		gitPasswordKey: e.gitToken,
	})
	if err != nil {
		slog.Warn("sync git auth secret failed, this build clones anonymously",
			"namespace", e.namespace, "secret", e.gitAuthSecret, "error", err)
		return ""
	}
	return e.gitAuthSecret
}

func (e *KanikoBuildExecutor) Submit(ctx context.Context, sub *port.BuildSubmission) (string, error) {
	jobName := fmt.Sprintf("kaniko-%s", strings.ReplaceAll(sub.BuildID, "-", ""))
	ttl := int32(3600)
	backoff := int32(0)
	// 从 Job 创建时刻起算，排不上队的时间也算在内 —— 这正是要的：
	// 没有期限时，调度不上的构建会永远停在 running（build 记录在 Submit 成功即写 running），
	// Makefile 的轮询只认终态，于是卡死。观测到最慢的真实构建 425s，给 4 倍余量。
	deadline := int64(1800)

	gitContext := "git://github.com/" + sub.GitRepo
	gitRef := sub.GitRef
	if gitRef != "" && !strings.HasPrefix(gitRef, "refs/") {
		if isCommitHash(gitRef) {
			// kaniko git context 直接使用 commit hash
		} else if looksLikeTag(gitRef) {
			gitRef = "refs/tags/" + gitRef
		} else {
			gitRef = "refs/heads/" + gitRef
		}
	}

	args := []string{
		fmt.Sprintf("--context=%s#%s", gitContext, gitRef),
		fmt.Sprintf("--destination=%s", sub.ImageTag),
	}
	if !sub.NoCache && e.cacheRepo != "" {
		args = append(args, "--cache=true", "--cache-repo="+e.cacheRepo)
	} else {
		args = append(args, "--cache=false")
	}
	args = append(args, "--snapshot-mode=redo")

	// 构建上下文子目录
	if sub.ContextDir != "" && sub.ContextDir != "." {
		args = append(args, fmt.Sprintf("--context-sub-path=%s", sub.ContextDir))
	}
	// 自定义 Dockerfile 路径
	if sub.Dockerfile != "" {
		args = append(args, fmt.Sprintf("--dockerfile=%s", sub.Dockerfile))
	}
	for _, mirror := range e.registryMirrors {
		args = append(args, fmt.Sprintf("--registry-mirror=%s", mirror))
	}
	for _, reg := range e.insecureRegistries {
		args = append(args, fmt.Sprintf("--insecure-registry=%s", reg))
		args = append(args, fmt.Sprintf("--skip-tls-verify-registry=%s", reg))
	}

	gitAuthSecret := e.syncGitAuthSecret(ctx)

	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      jobName,
			Namespace: e.namespace,
			Labels: map[string]string{
				labelBuildID: sub.BuildID,
			},
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            &backoff,
			TTLSecondsAfterFinished: &ttl,
			ActiveDeadlineSeconds:   &deadline,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{labelBuildID: sub.BuildID},
				},
				Spec: e.podSpec(args, gitAuthSecret),
			},
		},
	}

	if _, err := e.client.BatchV1().Jobs(e.namespace).Create(ctx, job, metav1.CreateOptions{}); err != nil {
		return "", err
	}
	return jobName, nil
}

func (e *KanikoBuildExecutor) podSpec(args []string, gitAuthSecret string) corev1.PodSpec {
	spec := corev1.PodSpec{
		RestartPolicy: corev1.RestartPolicyNever,
		// 构建需要外网（git clone + base 镜像），只有 app 节点有可用出口。
		// 与 deployer.go 对齐，别让 Job 漂到 proxy 节点上去。
		NodeSelector: map[string]string{"node-role": "app"},
		Containers: []corev1.Container{
			{
				Name:  "kaniko",
				Image: e.kanikoImage,
				Args:  args,
			},
		},
	}
	if e.httpProxy != "" {
		spec.Containers[0].Env = append(spec.Containers[0].Env,
			corev1.EnvVar{Name: "HTTP_PROXY", Value: e.httpProxy},
			corev1.EnvVar{Name: "HTTPS_PROXY", Value: e.httpProxy},
			corev1.EnvVar{Name: "http_proxy", Value: e.httpProxy},
			corev1.EnvVar{Name: "https_proxy", Value: e.httpProxy},
		)
		if e.noProxy != "" {
			spec.Containers[0].Env = append(spec.Containers[0].Env,
				corev1.EnvVar{Name: "NO_PROXY", Value: e.noProxy},
				corev1.EnvVar{Name: "no_proxy", Value: e.noProxy},
			)
		}
	}
	if gitAuthSecret != "" {
		spec.Containers[0].Env = append(spec.Containers[0].Env,
			gitAuthEnv(gitUsernameKey, gitAuthSecret),
			gitAuthEnv(gitPasswordKey, gitAuthSecret),
		)
	}
	if e.registrySecret != "" {
		volumeName := "docker-config"
		spec.Volumes = []corev1.Volume{
			{
				Name: volumeName,
				VolumeSource: corev1.VolumeSource{
					Secret: &corev1.SecretVolumeSource{
						SecretName: e.registrySecret,
						Items: []corev1.KeyToPath{
							{Key: ".dockerconfigjson", Path: "config.json"},
						},
					},
				},
			},
		}
		spec.Containers[0].VolumeMounts = []corev1.VolumeMount{
			{Name: volumeName, MountPath: "/kaniko/.docker", ReadOnly: true},
		}
	}
	return spec
}

// gitAuthEnv 让 kaniko 容器从 Secret 读一个 git 凭据变量，token 不以明文出现在 Job / Pod spec 里。
// optional：Pod 启动前 Secret 被删的话退回匿名克隆，而不是卡在 CreateContainerConfigError 直到 deadline。
func gitAuthEnv(key, secretName string) corev1.EnvVar {
	optional := true
	return corev1.EnvVar{
		Name: key,
		ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
				Key:                  key,
				Optional:             &optional,
			},
		},
	}
}

func (e *KanikoBuildExecutor) Cancel(ctx context.Context, jobName string) error {
	propagation := metav1.DeletePropagationForeground
	return e.client.BatchV1().Jobs(e.namespace).Delete(ctx, jobName, metav1.DeleteOptions{
		PropagationPolicy: &propagation,
	})
}

// Watch 启动 Job Informer，监听标签匹配的 Kaniko Job 状态变化。
func (e *KanikoBuildExecutor) Watch(ctx context.Context, callback port.BuildStatusCallback) error {
	factory := informers.NewSharedInformerFactoryWithOptions(
		e.client,
		0,
		informers.WithNamespace(e.namespace),
	)
	jobInformer := factory.Batch().V1().Jobs().Informer()

	jobInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		UpdateFunc: func(oldObj, newObj interface{}) {
			job, ok := newObj.(*batchv1.Job)
			if !ok {
				return
			}
			buildID, ok := job.Labels[labelBuildID]
			if !ok {
				return
			}

			status, log := jobToStatus(job)
			if status != "" {
				callback(buildID, status, log)
			}
		},
	})

	factory.Start(ctx.Done())
	factory.WaitForCacheSync(ctx.Done())

	<-ctx.Done()
	return ctx.Err()
}

// GetLogs 通过 buildID label 找到 Pod，读取容器日志。
func (e *KanikoBuildExecutor) GetLogs(ctx context.Context, buildID string) (string, error) {
	pods, err := e.client.CoreV1().Pods(e.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("%s=%s", labelBuildID, buildID),
	})
	if err != nil {
		return "", fmt.Errorf("list pods for build %s: %w", buildID, err)
	}
	if len(pods.Items) == 0 {
		return "", nil
	}

	pod := pods.Items[0]
	stream, err := e.client.CoreV1().Pods(e.namespace).GetLogs(pod.Name, &corev1.PodLogOptions{
		Container: "kaniko",
	}).Stream(ctx)
	if err != nil {
		return "", fmt.Errorf("get pod logs %s: %w", pod.Name, err)
	}
	defer stream.Close()

	data, err := io.ReadAll(stream)
	if err != nil {
		return "", fmt.Errorf("read pod logs %s: %w", pod.Name, err)
	}
	return string(data), nil
}

func isCommitHash(ref string) bool {
	if len(ref) < 7 || len(ref) > 40 {
		return false
	}
	for _, c := range ref {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func looksLikeTag(ref string) bool {
	return strings.HasPrefix(ref, "v") && len(ref) > 1 && ref[1] >= '0' && ref[1] <= '9'
}

func jobToStatus(job *batchv1.Job) (domain.BuildStatus, string) {
	for _, cond := range job.Status.Conditions {
		if cond.Type == batchv1.JobComplete && cond.Status == "True" {
			return domain.BuildStatusSucceeded, ""
		}
		if cond.Type == batchv1.JobFailed && cond.Status == "True" {
			return domain.BuildStatusFailed, cond.Message
		}
	}
	if job.Status.Active > 0 {
		return domain.BuildStatusRunning, ""
	}
	return "", ""
}
