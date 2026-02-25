package kubernetes

import (
	"context"
	"fmt"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
)

var _ port.VirtualServiceReconciler = (*IstioVirtualServiceReconciler)(nil)

var virtualServiceGVR = schema.GroupVersionResource{
	Group:    "networking.istio.io",
	Version:  "v1beta1",
	Resource: "virtualservices",
}

type IstioVirtualServiceReconciler struct {
	dynamic   dynamic.Interface
	namespace string
}

func NewIstioVirtualServiceReconciler(dynamic dynamic.Interface, namespace string) *IstioVirtualServiceReconciler {
	if namespace == "" {
		namespace = defaultNamespace
	}
	return &IstioVirtualServiceReconciler{dynamic: dynamic, namespace: namespace}
}

// Reconcile 根据 app 的所有 Release 重算 VirtualService 路由规则。
// 规则：x-lane header 精确匹配 → 对应 service，无匹配 → prod service（fallback）
func (r *IstioVirtualServiceReconciler) Reconcile(ctx context.Context, appName string, releases []*domain.Release) error {
	httpRoutes := buildHTTPRoutes(appName, releases)
	vs := buildVirtualService(appName, r.namespace, httpRoutes)

	existing, err := r.dynamic.Resource(virtualServiceGVR).Namespace(r.namespace).Get(ctx, appName, metav1.GetOptions{})
	if errors.IsNotFound(err) {
		_, err = r.dynamic.Resource(virtualServiceGVR).Namespace(r.namespace).Create(ctx, vs, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	vs.SetResourceVersion(existing.GetResourceVersion())
	_, err = r.dynamic.Resource(virtualServiceGVR).Namespace(r.namespace).Update(ctx, vs, metav1.UpdateOptions{})
	return err
}

func (r *IstioVirtualServiceReconciler) Delete(ctx context.Context, appName string) error {
	err := r.dynamic.Resource(virtualServiceGVR).Namespace(r.namespace).Delete(ctx, appName, metav1.DeleteOptions{})
	if errors.IsNotFound(err) {
		return nil
	}
	return err
}

func buildHTTPRoutes(appName string, releases []*domain.Release) []interface{} {
	var routes []interface{}

	// 非 prod 泳道：增加 x-lane header 匹配规则
	for _, rel := range releases {
		if rel.Lane == domain.DefaultLane {
			continue
		}
		route := map[string]interface{}{
			"match": []interface{}{
				map[string]interface{}{
					"headers": map[string]interface{}{
						"x-lane": map[string]interface{}{
							"exact": rel.Lane,
						},
					},
				},
			},
			"route": []interface{}{
				map[string]interface{}{
					"destination": map[string]interface{}{
						"host": fmt.Sprintf("%s-%s", appName, rel.Lane),
					},
				},
			},
		}
		routes = append(routes, route)
	}

	// 默认路由 → prod
	defaultRoute := map[string]interface{}{
		"route": []interface{}{
			map[string]interface{}{
				"destination": map[string]interface{}{
					"host": fmt.Sprintf("%s-%s", appName, domain.DefaultLane),
				},
			},
		},
	}
	routes = append(routes, defaultRoute)
	return routes
}

func buildVirtualService(appName, namespace string, httpRoutes []interface{}) *unstructured.Unstructured {
	return &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "networking.istio.io/v1beta1",
			"kind":       "VirtualService",
			"metadata": map[string]interface{}{
				"name":      appName,
				"namespace": namespace,
			},
			"spec": map[string]interface{}{
				"hosts": []interface{}{appName},
				"http":  httpRoutes,
			},
		},
	}
}
