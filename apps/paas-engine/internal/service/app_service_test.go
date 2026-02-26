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

func TestCreateApp_Success(t *testing.T) {
	appRepo := &stubAppRepo{}
	imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{Name: "myapp"}}
	svc := NewAppService(appRepo, imageRepoRepo, &stubReleaseRepo{})

	app, err := svc.CreateApp(context.Background(), CreateAppRequest{
		Name:          "myapp",
		ImageRepoName: "myapp",
		Port:          8080,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if app.ImageRepoName != "myapp" {
		t.Errorf("ImageRepoName = %q, want %q", app.ImageRepoName, "myapp")
	}
}

func TestCreateApp_ImageRepoNotFound(t *testing.T) {
	appRepo := &stubAppRepo{}
	imageRepoRepo := &stubImageRepoRepo{err: domain.ErrImageRepoNotFound}
	svc := NewAppService(appRepo, imageRepoRepo, &stubReleaseRepo{})

	_, err := svc.CreateApp(context.Background(), CreateAppRequest{
		Name:          "myapp",
		ImageRepoName: "nonexistent",
		Port:          8080,
	})
	if !errors.Is(err, domain.ErrNotFound) {
		t.Errorf("expected ErrNotFound, got %v", err)
	}
}

func TestCreateApp_InvalidName(t *testing.T) {
	appRepo := &stubAppRepo{}
	imageRepoRepo := &stubImageRepoRepo{}
	svc := NewAppService(appRepo, imageRepoRepo, &stubReleaseRepo{})

	_, err := svc.CreateApp(context.Background(), CreateAppRequest{
		Name: "INVALID",
		Port: 8080,
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestUpdateApp_Success(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:          "myapp",
		ImageRepoName: "myapp",
		Port:          8080,
	}}
	imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{Name: "myapp"}}
	svc := NewAppService(appRepo, imageRepoRepo, &stubReleaseRepo{})

	app, err := svc.UpdateApp(context.Background(), "myapp", UpdateAppRequest{
		ImageRepoName: "myapp",
		Port:          9090,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if app.Port != 9090 {
		t.Errorf("Port = %d, want %d", app.Port, 9090)
	}
}
