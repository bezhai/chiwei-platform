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
	if err := d.applyService(ctx, release, app); err != nil {
		return fmt.Errorf("apply service: %w", err)
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

	envVars := envsToK8s(mergeEnvs(app.Envs, release.Envs))
	replicas := release.Replicas

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
					Containers: []corev1.Container{
						{
							Name:  app.Name,
							Image: release.Image,
							Ports: []corev1.ContainerPort{
								{ContainerPort: int32(app.Port)},
							},
							EnvFrom: secretRefsToEnvFrom(app.EnvFromSecrets),
						Env:     envVars,
						},
					},
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

func secretRefsToEnvFrom(secrets []string) []corev1.EnvFromSource {
	if len(secrets) == 0 {
		return nil
	}
	sources := make([]corev1.EnvFromSource, len(secrets))
	for i, name := range secrets {
		sources[i] = corev1.EnvFromSource{
			SecretRef: &corev1.SecretEnvSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: name},
			},
		}
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
