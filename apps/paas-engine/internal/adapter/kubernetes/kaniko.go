package kubernetes

import (
	"context"
	"fmt"
	"io"
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

type KanikoBuildExecutor struct {
	client             kubernetes.Interface
	namespace          string
	kanikoImage        string
	registrySecret     string
	registryMirrors    []string
	insecureRegistries []string
	httpProxy          string
	noProxy            string
}

type KanikoBuildConfig struct {
	Namespace          string
	KanikoImage        string
	RegistrySecret     string
	RegistryMirrors    []string
	InsecureRegistries []string
	HttpProxy          string
	NoProxy            string
}

func NewKanikoBuildExecutor(client kubernetes.Interface, cfg KanikoBuildConfig) *KanikoBuildExecutor {
	return &KanikoBuildExecutor{
		client:             client,
		namespace:          cfg.Namespace,
		kanikoImage:        cfg.KanikoImage,
		registrySecret:     cfg.RegistrySecret,
		registryMirrors:    cfg.RegistryMirrors,
		insecureRegistries: cfg.InsecureRegistries,
		httpProxy:          cfg.HttpProxy,
		noProxy:            cfg.NoProxy,
	}
}

func (e *KanikoBuildExecutor) Submit(ctx context.Context, build *domain.Build) (string, error) {
	jobName := fmt.Sprintf("kaniko-%s", strings.ReplaceAll(build.ID, "-", ""))
	ttl := int32(3600)
	backoff := int32(0)

	gitContext := build.GitRepo
	if strings.HasPrefix(gitContext, "https://") || strings.HasPrefix(gitContext, "http://") {
		gitContext = "git://" + strings.TrimPrefix(strings.TrimPrefix(gitContext, "https://"), "http://")
	}
	gitRef := build.GitRef
	if gitRef != "" && !strings.HasPrefix(gitRef, "refs/") {
		// commit hash (hex, 7-40 chars) 不需要加前缀
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
		fmt.Sprintf("--destination=%s", build.ImageTag),
		"--cache=true",
	}

	// 指定子目录作为构建上下文，Kaniko 会在子目录下查找 Dockerfile
	if build.ContextDir != "" && build.ContextDir != "." {
		args = append(args, fmt.Sprintf("--context-sub-path=%s", build.ContextDir))
	}
	for _, mirror := range e.registryMirrors {
		args = append(args, fmt.Sprintf("--registry-mirror=%s", mirror))
	}
	for _, reg := range e.insecureRegistries {
		args = append(args, fmt.Sprintf("--insecure-registry=%s", reg))
		args = append(args, fmt.Sprintf("--skip-tls-verify-registry=%s", reg))
	}

	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      jobName,
			Namespace: e.namespace,
			Labels: map[string]string{
				labelBuildID: build.ID,
			},
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            &backoff,
			TTLSecondsAfterFinished: &ttl,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{labelBuildID: build.ID},
				},
				Spec: e.podSpec(args),
			},
		},
	}

	if _, err := e.client.BatchV1().Jobs(e.namespace).Create(ctx, job, metav1.CreateOptions{}); err != nil {
		return "", err
	}
	return jobName, nil
}

func (e *KanikoBuildExecutor) podSpec(args []string) corev1.PodSpec {
	spec := corev1.PodSpec{
		RestartPolicy: corev1.RestartPolicyNever,
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
