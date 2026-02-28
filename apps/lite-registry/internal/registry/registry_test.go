package registry

import (
	"context"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func makeSvc(name, namespace, app, lane string, port int32) *corev1.Service {
	labels := map[string]string{}
	if app != "" {
		labels["app"] = app
	}
	if lane != "" {
		labels["lane"] = lane
	}

	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels:    labels,
		},
	}
	if port > 0 {
		svc.Spec.Ports = []corev1.ServicePort{{Port: port}}
	}
	return svc
}

func startRegistry(t *testing.T, client *fake.Clientset, namespace string) *Registry {
	t.Helper()
	reg := New(client, namespace, 0)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	done := make(chan error, 1)
	go func() { done <- reg.Start(ctx) }()

	// Wait for ready
	deadline := time.After(5 * time.Second)
	for !reg.Ready() {
		select {
		case <-deadline:
			t.Fatal("registry did not become ready in time")
		case <-time.After(10 * time.Millisecond):
		}
	}
	return reg
}

func TestEmptyRegistry(t *testing.T) {
	client := fake.NewSimpleClientset()
	reg := startRegistry(t, client, "prod")

	snap := reg.Snapshot()
	if len(snap) != 0 {
		t.Errorf("expected empty snapshot, got %v", snap)
	}
}

func TestSingleLaneService(t *testing.T) {
	svc := makeSvc("myapp-prod", "prod", "myapp", "prod", 8080)
	client := fake.NewSimpleClientset(svc)
	reg := startRegistry(t, client, "prod")

	info, ok := reg.Get("myapp")
	if !ok {
		t.Fatal("expected myapp to exist")
	}
	if info.Port != 8080 {
		t.Errorf("expected port 8080, got %d", info.Port)
	}
	if len(info.Lanes) != 1 || info.Lanes[0] != "prod" {
		t.Errorf("expected lanes [prod], got %v", info.Lanes)
	}
}

func TestMultipleLanes(t *testing.T) {
	svcs := []corev1.Service{
		*makeSvc("myapp-prod", "prod", "myapp", "prod", 8080),
		*makeSvc("myapp-dev", "prod", "myapp", "dev", 8080),
		*makeSvc("myapp-blue", "prod", "myapp", "blue", 8080),
	}
	objs := make([]interface{}, len(svcs))
	for i := range svcs {
		objs[i] = &svcs[i]
	}

	client := fake.NewSimpleClientset(&svcs[0], &svcs[1], &svcs[2])
	reg := startRegistry(t, client, "prod")

	info, ok := reg.Get("myapp")
	if !ok {
		t.Fatal("expected myapp to exist")
	}
	if len(info.Lanes) != 3 {
		t.Fatalf("expected 3 lanes, got %v", info.Lanes)
	}
	// Lanes should be sorted
	expected := []string{"blue", "dev", "prod"}
	for i, l := range info.Lanes {
		if l != expected[i] {
			t.Errorf("lane[%d]: expected %s, got %s", i, expected[i], l)
		}
	}
}

func TestBaseServiceWithoutLaneLabel(t *testing.T) {
	// Base service has app label but no lane label — should be excluded
	baseSvc := makeSvc("myapp", "prod", "myapp", "", 8080)
	laneSvc := makeSvc("myapp-prod", "prod", "myapp", "prod", 8080)
	client := fake.NewSimpleClientset(baseSvc, laneSvc)
	reg := startRegistry(t, client, "prod")

	info, ok := reg.Get("myapp")
	if !ok {
		t.Fatal("expected myapp to exist")
	}
	// Only the lane service should be counted
	if len(info.Lanes) != 1 || info.Lanes[0] != "prod" {
		t.Errorf("expected lanes [prod], got %v", info.Lanes)
	}
}

func TestServiceWithNoPorts(t *testing.T) {
	svc := makeSvc("myworker-prod", "prod", "myworker", "prod", 0)
	client := fake.NewSimpleClientset(svc)
	reg := startRegistry(t, client, "prod")

	info, ok := reg.Get("myworker")
	if !ok {
		t.Fatal("expected myworker to exist")
	}
	if info.Port != 0 {
		t.Errorf("expected port 0, got %d", info.Port)
	}
}

func TestServiceWithoutAppLabel(t *testing.T) {
	// Service without app label should be ignored
	svc := makeSvc("random-svc", "prod", "", "prod", 80)
	client := fake.NewSimpleClientset(svc)
	reg := startRegistry(t, client, "prod")

	snap := reg.Snapshot()
	if len(snap) != 0 {
		t.Errorf("expected empty snapshot, got %v", snap)
	}
}

func TestDeleteService(t *testing.T) {
	svc := makeSvc("myapp-prod", "prod", "myapp", "prod", 8080)
	client := fake.NewSimpleClientset(svc)
	reg := startRegistry(t, client, "prod")

	// Verify it exists
	if _, ok := reg.Get("myapp"); !ok {
		t.Fatal("expected myapp to exist before delete")
	}

	// Delete the service
	err := client.CoreV1().Services("prod").Delete(context.Background(), "myapp-prod", metav1.DeleteOptions{})
	if err != nil {
		t.Fatalf("failed to delete service: %v", err)
	}

	// Wait for informer to process the delete event
	time.Sleep(500 * time.Millisecond)

	_, ok := reg.Get("myapp")
	if ok {
		t.Error("expected myapp to be gone after delete")
	}
}

func TestMultipleApps(t *testing.T) {
	client := fake.NewSimpleClientset(
		makeSvc("app1-prod", "prod", "app1", "prod", 8080),
		makeSvc("app2-prod", "prod", "app2", "prod", 3000),
		makeSvc("app2-dev", "prod", "app2", "dev", 3000),
	)
	reg := startRegistry(t, client, "prod")

	snap := reg.Snapshot()
	if len(snap) != 2 {
		t.Fatalf("expected 2 apps, got %d", len(snap))
	}

	if snap["app1"].Port != 8080 {
		t.Errorf("app1 port: expected 8080, got %d", snap["app1"].Port)
	}
	if snap["app2"].Port != 3000 {
		t.Errorf("app2 port: expected 3000, got %d", snap["app2"].Port)
	}
	if len(snap["app2"].Lanes) != 2 {
		t.Errorf("app2 lanes: expected 2, got %v", snap["app2"].Lanes)
	}
}

func TestSnapshotIsCopy(t *testing.T) {
	client := fake.NewSimpleClientset(
		makeSvc("myapp-prod", "prod", "myapp", "prod", 8080),
	)
	reg := startRegistry(t, client, "prod")

	snap := reg.Snapshot()
	// Mutate the returned snapshot
	snap["myapp"] = ServiceInfo{Port: 9999}
	snap["injected"] = ServiceInfo{Port: 1234}

	// Original should be unaffected
	info, _ := reg.Get("myapp")
	if info.Port != 8080 {
		t.Error("snapshot mutation affected registry state")
	}
	if _, ok := reg.Get("injected"); ok {
		t.Error("snapshot mutation injected into registry state")
	}
}
