package service

import (
	"context"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stubs for build tests ---

type stubBuildRepo struct {
	saved *domain.Build
}

func (s *stubBuildRepo) Save(_ context.Context, b *domain.Build) error {
	s.saved = b
	return nil
}
func (s *stubBuildRepo) FindByID(_ context.Context, _ string) (*domain.Build, error) {
	return nil, domain.ErrBuildNotFound
}
func (s *stubBuildRepo) FindByApp(_ context.Context, _ string) ([]*domain.Build, error) {
	return nil, nil
}
func (s *stubBuildRepo) Update(_ context.Context, _ *domain.Build) error { return nil }

func TestCreateBuild_ContextDirFallbackToApp(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:       "myapp",
		Image:      "registry.example.com",
		Port:       8080,
		ContextDir: "apps/myapp",
	}}
	buildRepo := &stubBuildRepo{}
	svc := NewBuildService(appRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRepo: "https://github.com/example/repo.git",
		GitRef:  "main",
		// ContextDir 留空，应回退到 App 默认值
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.ContextDir != "apps/myapp" {
		t.Errorf("ContextDir = %q, want %q", build.ContextDir, "apps/myapp")
	}
}

func TestCreateBuild_ContextDirFallbackToDot(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:  "myapp",
		Image: "registry.example.com",
		Port:  8080,
		// ContextDir 为空
	}}
	buildRepo := &stubBuildRepo{}
	svc := NewBuildService(appRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRepo: "https://github.com/example/repo.git",
		GitRef:  "main",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.ContextDir != "." {
		t.Errorf("ContextDir = %q, want %q", build.ContextDir, ".")
	}
}

func TestCreateBuild_ContextDirExplicitOverridesApp(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:       "myapp",
		Image:      "registry.example.com",
		Port:       8080,
		ContextDir: "apps/myapp",
	}}
	buildRepo := &stubBuildRepo{}
	svc := NewBuildService(appRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRepo:    "https://github.com/example/repo.git",
		GitRef:     "main",
		ContextDir: "custom/path",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.ContextDir != "custom/path" {
		t.Errorf("ContextDir = %q, want %q", build.ContextDir, "custom/path")
	}
}
