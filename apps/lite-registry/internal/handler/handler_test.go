package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chiwei-platform/lite-registry/internal/registry"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func makeSvc(name, app, lane string, port int32) *corev1.Service {
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
			Namespace: "prod",
			Labels:    labels,
		},
	}
	if port > 0 {
		svc.Spec.Ports = []corev1.ServicePort{{Port: port}}
	}
	return svc
}

func readyRegistry(t *testing.T, svcs ...*corev1.Service) *registry.Registry {
	t.Helper()
	client := fake.NewSimpleClientset()
	for _, svc := range svcs {
		_, err := client.CoreV1().Services("prod").Create(context.Background(), svc, metav1.CreateOptions{})
		if err != nil {
			t.Fatalf("failed to create service: %v", err)
		}
	}

	reg := registry.New(client, "prod", 0)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	go reg.Start(ctx)

	deadline := time.After(5 * time.Second)
	for !reg.Ready() {
		select {
		case <-deadline:
			t.Fatal("registry did not become ready")
		case <-time.After(10 * time.Millisecond):
		}
	}
	return reg
}

func TestHealthz(t *testing.T) {
	reg := readyRegistry(t)
	router := NewRouter(reg)

	req := httptest.NewRequest("GET", "/healthz", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}

	var body map[string]string
	json.NewDecoder(w.Body).Decode(&body)
	if body["status"] != "ok" {
		t.Errorf("expected status ok, got %s", body["status"])
	}
}

func TestReadyzNotReady(t *testing.T) {
	client := fake.NewSimpleClientset()
	reg := registry.New(client, "prod", 0)
	router := NewRouter(reg)

	req := httptest.NewRequest("GET", "/readyz", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503, got %d", w.Code)
	}
}

func TestReadyzReady(t *testing.T) {
	reg := readyRegistry(t)
	router := NewRouter(reg)

	req := httptest.NewRequest("GET", "/readyz", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestListRoutes(t *testing.T) {
	reg := readyRegistry(t,
		makeSvc("myapp-prod", "myapp", "prod", 8080),
		makeSvc("myapp-dev", "myapp", "dev", 8080),
	)
	router := NewRouter(reg)

	req := httptest.NewRequest("GET", "/v1/routes", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}

	var resp RoutesResponse
	json.NewDecoder(w.Body).Decode(&resp)

	info, ok := resp.Services["myapp"]
	if !ok {
		t.Fatal("expected myapp in routes")
	}
	if info.Port != 8080 {
		t.Errorf("expected port 8080, got %d", info.Port)
	}
	if len(info.Lanes) != 2 {
		t.Errorf("expected 2 lanes, got %v", info.Lanes)
	}
}

func TestGetRouteExists(t *testing.T) {
	reg := readyRegistry(t,
		makeSvc("myapp-prod", "myapp", "prod", 3000),
	)
	router := NewRouter(reg)

	req := httptest.NewRequest("GET", "/v1/routes/myapp", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}

	var info registry.ServiceInfo
	json.NewDecoder(w.Body).Decode(&info)
	if info.Port != 3000 {
		t.Errorf("expected port 3000, got %d", info.Port)
	}
}

func TestGetRouteNotFound(t *testing.T) {
	reg := readyRegistry(t)
	router := NewRouter(reg)

	req := httptest.NewRequest("GET", "/v1/routes/nonexistent", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("expected 404, got %d", w.Code)
	}
}
