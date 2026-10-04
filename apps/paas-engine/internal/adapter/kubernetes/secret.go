package kubernetes

import (
	"context"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// applySecret 创建或更新一个由 paas-engine 维护的 Secret（带 managed-by 标签）。
// 只用 get / create / update 三个动词：paas-builds 的 Role 对 secrets 只给了这三个，
// 换成 patch / apply 在那里会被 RBAC 拒绝。
func applySecret(ctx context.Context, client kubernetes.Interface, namespace, name string, data map[string]string) error {
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels:    map[string]string{"managed-by": "paas-engine"},
		},
		StringData: data,
	}

	existing, err := client.CoreV1().Secrets(namespace).Get(ctx, name, metav1.GetOptions{})
	if errors.IsNotFound(err) {
		_, err = client.CoreV1().Secrets(namespace).Create(ctx, secret, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	existing.StringData = data
	_, err = client.CoreV1().Secrets(namespace).Update(ctx, existing, metav1.UpdateOptions{})
	return err
}
