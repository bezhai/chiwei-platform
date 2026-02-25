package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stubs for app tests ---

type stubReleaseRepo struct{}

func (s *stubReleaseRepo) Save(_ context.Context, _ *domain.Release) error   { return nil }
func (s *stubReleaseRepo) Update(_ context.Context, _ *domain.Release) error { return nil }
func (s *stubReleaseRepo) Delete(_ context.Context, _ string) error          { return nil }
func (s *stubReleaseRepo) FindByID(_ context.Context, _ string) (*domain.Release, error) {
	return nil, nil
}
func (s *stubReleaseRepo) FindByAppAndLane(_ context.Context, _, _ string) (*domain.Release, error) {
	return nil, nil
}
func (s *stubReleaseRepo) FindAll(_ context.Context, _, _ string) ([]*domain.Release, error) {
	return nil, nil
}
func (s *stubReleaseRepo) FindByLane(_ context.Context, _ string) ([]*domain.Release, error) {
	return nil, nil
}

func TestCreateApp_WithContextDir(t *testing.T) {
	appRepo := &stubAppRepo{}
	svc := NewAppService(appRepo, &stubReleaseRepo{})

	app, err := svc.CreateApp(context.Background(), CreateAppRequest{
		Name:       "myapp",
		Image:      "registry.example.com",
		Port:       8080,
		ContextDir: "apps/myapp",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if app.ContextDir != "apps/myapp" {
		t.Errorf("ContextDir = %q, want %q", app.ContextDir, "apps/myapp")
	}
}

func TestCreateApp_InvalidContextDir(t *testing.T) {
	appRepo := &stubAppRepo{}
	svc := NewAppService(appRepo, &stubReleaseRepo{})

	_, err := svc.CreateApp(context.Background(), CreateAppRequest{
		Name:       "myapp",
		Image:      "registry.example.com",
		Port:       8080,
		ContextDir: "../etc/passwd",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestUpdateApp_WithContextDir(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:  "myapp",
		Image: "registry.example.com",
		Port:  8080,
	}}
	svc := NewAppService(appRepo, &stubReleaseRepo{})

	app, err := svc.UpdateApp(context.Background(), "myapp", UpdateAppRequest{
		Image:      "registry.example.com",
		Port:       8080,
		ContextDir: "apps/myapp",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if app.ContextDir != "apps/myapp" {
		t.Errorf("ContextDir = %q, want %q", app.ContextDir, "apps/myapp")
	}
}

func TestUpdateApp_InvalidContextDir(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:  "myapp",
		Image: "registry.example.com",
		Port:  8080,
	}}
	svc := NewAppService(appRepo, &stubReleaseRepo{})

	_, err := svc.UpdateApp(context.Background(), "myapp", UpdateAppRequest{
		Image:      "registry.example.com",
		Port:       8080,
		ContextDir: "/absolute/path",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}
