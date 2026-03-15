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

var _ port.TestExecutor = (*K8sTestExecutor)(nil)

const labelJobRunID = "paas.chiwei/jobrun-id"

// runtimeImages 定义每种 runtime 对应的测试镜像（使用 Harbor 内部镜像）。
var runtimeImages = map[string]string{
	"go":     "harbor.local:30002/library/golang:1.25-alpine",
	"python": "harbor.local:30002/library/python:3.11-slim",
	"bun":    "harbor.local:30002/library/bun:1.3.9-slim",
}

type K8sTestExecutor struct {
	client    kubernetes.Interface
	namespace string
	gitRepo   string // monorepo 的 git 地址
	httpProxy string
	noProxy   string
}

type TestExecutorConfig struct {
	Namespace string
	GitRepo   string
	HttpProxy string
	NoProxy   string
}

func NewK8sTestExecutor(client kubernetes.Interface, cfg TestExecutorConfig) *K8sTestExecutor {
	return &K8sTestExecutor{
		client:    client,
		namespace: cfg.Namespace,
		gitRepo:   cfg.GitRepo,
		httpProxy: cfg.HttpProxy,
		noProxy:   cfg.NoProxy,
	}
}

func (e *K8sTestExecutor) Submit(ctx context.Context, sub *port.TestSubmission) (string, error) {
	jobName := fmt.Sprintf("ci-test-%s", strings.ReplaceAll(sub.JobRunID, "-", "")[:24])
	ttl := int32(3600)
	backoff := int32(0)

	image, ok := runtimeImages[sub.Runtime]
	if !ok {
		return "", fmt.Errorf("unsupported runtime %q", sub.Runtime)
	}

	// 拼装 https:// 前缀用于 git clone
	gitURL := "https://github.com/" + sub.GitRepo
	gitRef := sub.GitRef
	if gitRef == "" {
		gitRef = "main"
	}

	// Init container: clone repo
	initContainer := corev1.Container{
		Name:    "git-clone",
		Image:   "harbor.local:30002/library/alpine-git:latest",
		Command: []string{"sh", "-c"},
		Args: []string{
			fmt.Sprintf("git clone --depth 1 --branch '%s' '%s' /workspace", gitRef, gitURL),
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: "workspace", MountPath: "/workspace"},
		},
	}

	// Main container: run test command
	mainContainer := corev1.Container{
		Name:       "test",
		Image:      image,
		Command:    []string{"sh", "-c"},
		Args:       []string{sub.Command},
		WorkingDir: "/workspace",
		VolumeMounts: []corev1.VolumeMount{
			{Name: "workspace", MountPath: "/workspace"},
		},
	}

	// 注入环境变量
	for k, v := range sub.Envs {
		mainContainer.Env = append(mainContainer.Env, corev1.EnvVar{Name: k, Value: v})
	}

	// 代理设置
	if e.httpProxy != "" {
		proxyEnvs := []corev1.EnvVar{
			{Name: "HTTP_PROXY", Value: e.httpProxy},
			{Name: "HTTPS_PROXY", Value: e.httpProxy},
			{Name: "http_proxy", Value: e.httpProxy},
			{Name: "https_proxy", Value: e.httpProxy},
		}
		if e.noProxy != "" {
			proxyEnvs = append(proxyEnvs,
				corev1.EnvVar{Name: "NO_PROXY", Value: e.noProxy},
				corev1.EnvVar{Name: "no_proxy", Value: e.noProxy},
			)
		}
		initContainer.Env = append(initContainer.Env, proxyEnvs...)
		mainContainer.Env = append(mainContainer.Env, proxyEnvs...)
	}

	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      jobName,
			Namespace: e.namespace,
			Labels: map[string]string{
				labelJobRunID: sub.JobRunID,
			},
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            &backoff,
			TTLSecondsAfterFinished: &ttl,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{labelJobRunID: sub.JobRunID},
				},
				Spec: corev1.PodSpec{
					RestartPolicy:  corev1.RestartPolicyNever,
					InitContainers: []corev1.Container{initContainer},
					Containers:     []corev1.Container{mainContainer},
					Volumes: []corev1.Volume{
						{
							Name: "workspace",
							VolumeSource: corev1.VolumeSource{
								EmptyDir: &corev1.EmptyDirVolumeSource{},
							},
						},
					},
				},
			},
		},
	}

	if _, err := e.client.BatchV1().Jobs(e.namespace).Create(ctx, job, metav1.CreateOptions{}); err != nil {
		return "", err
	}
	return jobName, nil
}

func (e *K8sTestExecutor) Cancel(ctx context.Context, jobName string) error {
	propagation := metav1.DeletePropagationForeground
	return e.client.BatchV1().Jobs(e.namespace).Delete(ctx, jobName, metav1.DeleteOptions{
		PropagationPolicy: &propagation,
	})
}

// Watch 启动 Job Informer，监听标签匹配的测试 Job 状态变化。
func (e *K8sTestExecutor) Watch(ctx context.Context, callback port.TestStatusCallback) error {
	factory := informers.NewSharedInformerFactoryWithOptions(
		e.client,
		0,
		informers.WithNamespace(e.namespace),
	)
	jobInformer := factory.Batch().V1().Jobs().Informer()

	jobInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		UpdateFunc: func(oldObj, newObj any) {
			job, ok := newObj.(*batchv1.Job)
			if !ok {
				return
			}
			jobRunID, ok := job.Labels[labelJobRunID]
			if !ok {
				return
			}

			status, log := testJobToStatus(job)
			if status != "" {
				callback(jobRunID, status, log)
			}
		},
	})

	factory.Start(ctx.Done())
	factory.WaitForCacheSync(ctx.Done())

	<-ctx.Done()
	return ctx.Err()
}

// GetLogs 通过 jobRunID label 找到 Pod，读取 test 容器日志。
func (e *K8sTestExecutor) GetLogs(ctx context.Context, jobRunID string) (string, error) {
	pods, err := e.client.CoreV1().Pods(e.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("%s=%s", labelJobRunID, jobRunID),
	})
	if err != nil {
		return "", fmt.Errorf("list pods for job run %s: %w", jobRunID, err)
	}
	if len(pods.Items) == 0 {
		return "", nil
	}

	pod := pods.Items[0]
	containerName := "test"
	stream, err := e.client.CoreV1().Pods(e.namespace).GetLogs(pod.Name, &corev1.PodLogOptions{
		Container: containerName,
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

func testJobToStatus(job *batchv1.Job) (domain.PipelineRunStatus, string) {
	for _, cond := range job.Status.Conditions {
		if cond.Type == batchv1.JobComplete && cond.Status == "True" {
			return domain.PipelineRunSucceeded, ""
		}
		if cond.Type == batchv1.JobFailed && cond.Status == "True" {
			return domain.PipelineRunFailed, cond.Message
		}
	}
	if job.Status.Active > 0 {
		return domain.PipelineRunRunning, ""
	}
	return "", ""
}
