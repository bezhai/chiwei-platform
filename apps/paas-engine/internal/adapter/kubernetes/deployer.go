package kubernetes

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/client-go/kubernetes"
)

var _ port.Deployer = (*K8sDeployer)(nil)

const defaultNamespace = "default"

type K8sDeployer struct {
	client    kubernetes.Interface
	namespace string
}

func NewK8sDeployer(client kubernetes.Interface, namespace string) *K8sDeployer {
	if namespace == "" {
		namespace = defaultNamespace
	}
	return &K8sDeployer{client: client, namespace: namespace}
}

func (d *K8sDeployer) Deploy(ctx context.Context, release *domain.Release, app *domain.App) error {
	if err := d.applyDeployment(ctx, release, app); err != nil {
		return fmt.Errorf("apply deployment: %w", err)
	}
	if app.Port > 0 { // Worker 无端口，跳过 Service
		if err := d.applyService(ctx, release, app); err != nil {
			return fmt.Errorf("apply service: %w", err)
		}
		if err := d.applyBaseService(ctx, release, app); err != nil {
			return fmt.Errorf("apply base service: %w", err)
		}
	}
	if err := d.waitForRollout(ctx, release.ResourceName()); err != nil {
		return fmt.Errorf("wait for rollout: %w", err)
	}
	return nil
}

func (d *K8sDeployer) Delete(ctx context.Context, release *domain.Release) error {
	name := release.ResourceName()
	if err := d.client.AppsV1().Deployments(d.namespace).Delete(ctx, name, metav1.DeleteOptions{}); err != nil && !errors.IsNotFound(err) {
		return fmt.Errorf("delete deployment %s: %w", name, err)
	}
	if err := d.client.CoreV1().Services(d.namespace).Delete(ctx, name, metav1.DeleteOptions{}); err != nil && !errors.IsNotFound(err) {
		return fmt.Errorf("delete service %s: %w", name, err)
	}
	return nil
}

func (d *K8sDeployer) applyDeployment(ctx context.Context, release *domain.Release, app *domain.App) error {
	name := release.ResourceName()
	labels := map[string]string{
		"app":  release.AppName,
		"lane": release.Lane,
	}

	mergedEnvs := mergeEnvs(app.Envs, release.Envs)
	if release.Version != "" {
		mergedEnvs["VERSION"] = release.Version
	}
	envVars := envsToK8s(mergedEnvs)
	replicas := release.Replicas

	container := corev1.Container{
		Name:    app.Name,
		Image:   release.Image,
		EnvFrom: buildEnvFrom(app.EnvFromSecrets, app.EnvFromConfigMaps),
		Env:     envVars,
	}
	if len(app.Command) > 0 {
		container.Command = app.Command
	}
	if app.Port > 0 {
		container.Ports = []corev1.ContainerPort{
			{ContainerPort: int32(app.Port)},
		}
	}

	revisionHistoryLimit := int32(2)
	deploy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: d.namespace,
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas:             &replicas,
			RevisionHistoryLimit: &revisionHistoryLimit,
			Selector:             &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					ServiceAccountName: app.ServiceAccount,
					Containers:         []corev1.Container{container},
				},
			},
		},
	}

	existing, err := d.client.AppsV1().Deployments(d.namespace).Get(ctx, name, metav1.GetOptions{})
	if errors.IsNotFound(err) {
		_, err = d.client.AppsV1().Deployments(d.namespace).Create(ctx, deploy, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	existing.Spec = deploy.Spec
	_, err = d.client.AppsV1().Deployments(d.namespace).Update(ctx, existing, metav1.UpdateOptions{})
	return err
}

func (d *K8sDeployer) applyService(ctx context.Context, release *domain.Release, app *domain.App) error {
	name := release.ResourceName()
	labels := map[string]string{
		"app":  release.AppName,
		"lane": release.Lane,
	}

	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: d.namespace,
			Labels:    labels,
		},
		Spec: corev1.ServiceSpec{
			Selector: labels,
			Ports: []corev1.ServicePort{
				{
					Port:       int32(app.Port),
					TargetPort: intstr.FromInt(app.Port),
				},
			},
		},
	}

	existing, err := d.client.CoreV1().Services(d.namespace).Get(ctx, name, metav1.GetOptions{})
	if errors.IsNotFound(err) {
		_, err = d.client.CoreV1().Services(d.namespace).Create(ctx, svc, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	existing.Spec.Ports = svc.Spec.Ports
	_, err = d.client.CoreV1().Services(d.namespace).Update(ctx, existing, metav1.UpdateOptions{})
	return err
}

// applyBaseService 创建或更新 base Service（name=appName，无 lane 后缀）。
// selector 默认指向 prod lane，作为 sidecar 不在时的 fallback；
// 当 Istio sidecar 注入后，VirtualService 根据 x-lane header 路由到对应 lane。
func (d *K8sDeployer) applyBaseService(ctx context.Context, release *domain.Release, app *domain.App) error {
	name := release.AppName
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: d.namespace,
			Labels: map[string]string{
				"app": release.AppName,
			},
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{
				"app":  release.AppName,
				"lane": "prod",
			},
			Ports: []corev1.ServicePort{
				{
					Port:       int32(app.Port),
					TargetPort: intstr.FromInt(app.Port),
				},
			},
		},
	}

	existing, err := d.client.CoreV1().Services(d.namespace).Get(ctx, name, metav1.GetOptions{})
	if errors.IsNotFound(err) {
		_, err = d.client.CoreV1().Services(d.namespace).Create(ctx, svc, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	existing.Spec.Ports = svc.Spec.Ports
	_, err = d.client.CoreV1().Services(d.namespace).Update(ctx, existing, metav1.UpdateOptions{})
	return err
}

func mergeEnvs(base, override map[string]string) map[string]string {
	merged := make(map[string]string)
	for k, v := range base {
		merged[k] = v
	}
	for k, v := range override {
		merged[k] = v
	}
	return merged
}

// buildEnvFrom 合并 Secret 和 ConfigMap 的 envFrom sources。
func buildEnvFrom(secrets, configMaps []string) []corev1.EnvFromSource {
	sources := make([]corev1.EnvFromSource, 0, len(secrets)+len(configMaps))
	for _, name := range secrets {
		sources = append(sources, corev1.EnvFromSource{
			SecretRef: &corev1.SecretEnvSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: name},
			},
		})
	}
	for _, name := range configMaps {
		sources = append(sources, corev1.EnvFromSource{
			ConfigMapRef: &corev1.ConfigMapEnvSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: name},
			},
		})
	}
	if len(sources) == 0 {
		return nil
	}
	return sources
}

func envsToK8s(envs map[string]string) []corev1.EnvVar {
	result := make([]corev1.EnvVar, 0, len(envs))
	for k, v := range envs {
		result = append(result, corev1.EnvVar{Name: k, Value: v})
	}
	return result
}

const (
	rolloutTimeout  = 5 * time.Minute
	rolloutInterval = 3 * time.Second
)

// waitForRollout 轮询 Deployment 直到所有副本就绪或超时。
// 除了检查 Deployment 级别状态，还会检测 Pod 级别的 CrashLoopBackOff 以快速失败。
func (d *K8sDeployer) waitForRollout(ctx context.Context, name string) error {
	ctx, cancel := context.WithTimeout(ctx, rolloutTimeout)
	defer cancel()

	ticker := time.NewTicker(rolloutInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return fmt.Errorf("deployment %s rollout timed out after %s", name, rolloutTimeout)
		case <-ticker.C:
			deploy, err := d.client.AppsV1().Deployments(d.namespace).Get(ctx, name, metav1.GetOptions{})
			if err != nil {
				return fmt.Errorf("get deployment %s: %w", name, err)
			}

			// Progressing condition 为 False 表示部署卡住
			for _, cond := range deploy.Status.Conditions {
				if cond.Type == appsv1.DeploymentProgressing && cond.Status == corev1.ConditionFalse {
					return fmt.Errorf("deployment %s is not progressing: %s", name, cond.Message)
				}
			}

			// 检测 Pod 级别的 CrashLoopBackOff，快速失败而非等待超时
			if reason, ok := d.detectPodFailure(ctx, deploy); ok {
				return fmt.Errorf("deployment %s failed: %s", name, reason)
			}

			spec := deploy.Spec
			status := deploy.Status
			if status.ObservedGeneration >= deploy.Generation &&
				status.UpdatedReplicas == *spec.Replicas &&
				status.AvailableReplicas == *spec.Replicas {
				slog.Info("deployment rollout complete", "name", name)
				return nil
			}
		}
	}
}

// detectPodFailure 检查 Deployment 最新 ReplicaSet 的 Pod 是否存在不可恢复的失败状态。
// 返回失败原因和是否检测到失败。
func (d *K8sDeployer) detectPodFailure(ctx context.Context, deploy *appsv1.Deployment) (string, bool) {
	// 找到最新 ReplicaSet 的 pod-template-hash
	latestHash := d.getLatestRSHash(ctx, deploy)
	if latestHash == "" {
		return "", false
	}

	selector := deploy.Spec.Selector.MatchLabels
	labelSelector := ""
	for k, v := range selector {
		if labelSelector != "" {
			labelSelector += ","
		}
		labelSelector += k + "=" + v
	}
	// 直接用 label selector 过滤到最新 RS 的 Pod
	labelSelector += ",pod-template-hash=" + latestHash

	pods, err := d.client.CoreV1().Pods(d.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: labelSelector,
	})
	if err != nil {
		slog.Warn("failed to list pods for crash detection", "error", err)
		return "", false
	}

	for _, pod := range pods.Items {
		for _, cs := range pod.Status.ContainerStatuses {
			if cs.State.Waiting != nil && cs.State.Waiting.Reason == "CrashLoopBackOff" {
				return fmt.Sprintf("pod %s is in CrashLoopBackOff: %s",
					pod.Name, cs.State.Waiting.Message), true
			}
			if cs.State.Waiting != nil && cs.State.Waiting.Reason == "ImagePullBackOff" {
				return fmt.Sprintf("pod %s failed to pull image: %s",
					pod.Name, cs.State.Waiting.Message), true
			}
		}

		for _, cs := range pod.Status.InitContainerStatuses {
			if cs.State.Waiting != nil && cs.State.Waiting.Reason == "CrashLoopBackOff" {
				return fmt.Sprintf("pod %s init container is in CrashLoopBackOff: %s",
					pod.Name, cs.State.Waiting.Message), true
			}
		}
	}

	return "", false
}

// getLatestRSHash 获取 Deployment 最新 ReplicaSet 的 pod-template-hash。
// 最新 RS 通过 revision annotation 判断。
func (d *K8sDeployer) getLatestRSHash(ctx context.Context, deploy *appsv1.Deployment) string {
	selector := deploy.Spec.Selector.MatchLabels
	labelSelector := ""
	for k, v := range selector {
		if labelSelector != "" {
			labelSelector += ","
		}
		labelSelector += k + "=" + v
	}

	rsList, err := d.client.AppsV1().ReplicaSets(d.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: labelSelector,
	})
	if err != nil {
		slog.Warn("failed to list replicasets for hash lookup", "error", err)
		return ""
	}

	var latestRevision int64
	var latestHash string
	for _, rs := range rsList.Items {
		revStr := rs.Annotations["deployment.kubernetes.io/revision"]
		if revStr == "" {
			continue
		}
		var rev int64
		if _, err := fmt.Sscanf(revStr, "%d", &rev); err != nil {
			continue
		}
		if rev > latestRevision {
			latestRevision = rev
			latestHash = rs.Labels["pod-template-hash"]
		}
	}
	return latestHash
}
