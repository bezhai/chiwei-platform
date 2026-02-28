package service

import (
	"context"
	"errors"
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
func (s *stubBuildRepo) FindByImageRepo(_ context.Context, _ string) ([]*domain.Build, error) {
	return nil, nil
}
func (s *stubBuildRepo) FindLatestSuccessful(_ context.Context, _ string) (*domain.Build, error) {
	return nil, domain.ErrBuildNotFound
}
func (s *stubBuildRepo) Update(_ context.Context, _ *domain.Build) error { return nil }

func TestCreateBuild_UsesImageRepoConfig(t *testing.T) {
	imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{
		Name:       "agent-service",
		Registry:   "harbor.local/inner-bot/agent-service",
		GitRepo:    "https://github.com/bezhai/chiwei-platform.git",
		ContextDir: "apps/agent-service",
	}}
	buildRepo := &stubBuildRepo{}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "agent-service", CreateBuildRequest{
		GitRef:   "main",
		ImageTag: "abc123",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.ImageRepoName != "agent-service" {
		t.Errorf("ImageRepoName = %q, want %q", build.ImageRepoName, "agent-service")
	}
	if build.ImageTag != "harbor.local/inner-bot/agent-service:abc123" {
		t.Errorf("ImageTag = %q, want %q", build.ImageTag, "harbor.local/inner-bot/agent-service:abc123")
	}
}

func TestCreateBuild_DefaultTagFromGitRef(t *testing.T) {
	imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{
		Name:     "myapp",
		Registry: "harbor.local/inner-bot/myapp",
		GitRepo:  "https://github.com/example/repo.git",
	}}
	buildRepo := &stubBuildRepo{}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef: "feature-branch",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.ImageTag != "harbor.local/inner-bot/myapp:feature-branch" {
		t.Errorf("ImageTag = %q, want %q", build.ImageTag, "harbor.local/inner-bot/myapp:feature-branch")
	}
}

func TestCreateBuild_ImageRepoNotFound(t *testing.T) {
	imageRepoRepo := &stubImageRepoRepo{err: domain.ErrImageRepoNotFound}
	buildRepo := &stubBuildRepo{}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	_, err := svc.CreateBuild(context.Background(), "nonexistent", CreateBuildRequest{
		GitRef: "main",
	})
	if !errors.Is(err, domain.ErrNotFound) {
		t.Errorf("expected ErrNotFound, got %v", err)
	}
}
