package kubernetes

import (
	"context"
	"testing"

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
			deployer := NewK8sDeployer(client, "default")

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
