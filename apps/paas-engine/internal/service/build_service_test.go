package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stubs for build tests ---

type stubBuildRepo struct {
	saved          *domain.Build
	latestVersiond *domain.Build // FindLatestVersioned 返回
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
func (s *stubBuildRepo) FindLatestVersioned(_ context.Context, _ string) (*domain.Build, error) {
	if s.latestVersiond != nil {
		return s.latestVersiond, nil
	}
	return nil, domain.ErrBuildNotFound
}
func (s *stubBuildRepo) FindByImageTag(_ context.Context, _ string) (*domain.Build, error) {
	return nil, domain.ErrBuildNotFound
}
func (s *stubBuildRepo) Update(_ context.Context, _ *domain.Build) error { return nil }

func newTestImageRepoRepo(name, registry string) *stubImageRepoRepo {
	return &stubImageRepoRepo{repo: &domain.ImageRepo{
		Name:     name,
		Registry: registry,
		GitRepo:  "https://github.com/example/repo.git",
	}}
}

func TestCreateBuild_InitialVersion(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("agent-service", "harbor.local/inner-bot/agent-service")
	buildRepo := &stubBuildRepo{}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "agent-service", CreateBuildRequest{
		GitRef: "main",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.Version != "1.0.0.1" {
		t.Errorf("Version = %q, want %q", build.Version, "1.0.0.1")
	}
	if build.ImageTag != "harbor.local/inner-bot/agent-service:1.0.0.1" {
		t.Errorf("ImageTag = %q, want %q", build.ImageTag, "harbor.local/inner-bot/agent-service:1.0.0.1")
	}
	if build.Channel != domain.ChannelStable {
		t.Errorf("Channel = %q, want %q", build.Channel, domain.ChannelStable)
	}
}

func TestCreateBuild_AutoIncrementBuild(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
	buildRepo := &stubBuildRepo{
		latestVersiond: &domain.Build{Version: "1.0.0.3"},
	}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef: "feature-branch",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.Version != "1.0.0.4" {
		t.Errorf("Version = %q, want %q", build.Version, "1.0.0.4")
	}
	if build.Channel != domain.ChannelTest {
		t.Errorf("Channel = %q, want %q", build.Channel, domain.ChannelTest)
	}
}

func TestCreateBuild_BumpPatch(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
	buildRepo := &stubBuildRepo{
		latestVersiond: &domain.Build{Version: "1.0.0.5"},
	}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef: "main",
		Bump:   "patch",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.Version != "1.0.1.1" {
		t.Errorf("Version = %q, want %q", build.Version, "1.0.1.1")
	}
}

func TestCreateBuild_BumpMinor(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
	buildRepo := &stubBuildRepo{
		latestVersiond: &domain.Build{Version: "1.0.2.5"},
	}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef: "main",
		Bump:   "minor",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.Version != "1.1.0.1" {
		t.Errorf("Version = %q, want %q", build.Version, "1.1.0.1")
	}
}

func TestCreateBuild_BumpMajor(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
	buildRepo := &stubBuildRepo{
		latestVersiond: &domain.Build{Version: "1.2.3.5"},
	}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef: "main",
		Bump:   "major",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.Version != "2.0.0.1" {
		t.Errorf("Version = %q, want %q", build.Version, "2.0.0.1")
	}
}

func TestCreateBuild_ExplicitVersion(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
	buildRepo := &stubBuildRepo{
		latestVersiond: &domain.Build{Version: "1.0.0.3"},
	}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef:  "main",
		Version: "2.0.0.1",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if build.Version != "2.0.0.1" {
		t.Errorf("Version = %q, want %q", build.Version, "2.0.0.1")
	}
}

func TestCreateBuild_ExplicitVersionTooLow(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
	buildRepo := &stubBuildRepo{
		latestVersiond: &domain.Build{Version: "1.0.0.3"},
	}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	_, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef:  "main",
		Version: "1.0.0.2",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestCreateBuild_ExplicitVersionEqualCurrent(t *testing.T) {
	imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
	buildRepo := &stubBuildRepo{
		latestVersiond: &domain.Build{Version: "1.0.0.3"},
	}
	svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

	_, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
		GitRef:  "main",
		Version: "1.0.0.3",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestCreateBuild_ChannelFromGitRef(t *testing.T) {
	tests := []struct {
		gitRef      string
		wantChannel string
	}{
		{"main", domain.ChannelStable},
		{"develop", domain.ChannelTest},
		{"feature/foo", domain.ChannelTest},
	}
	for _, tt := range tests {
		imageRepoRepo := newTestImageRepoRepo("myapp", "harbor.local/inner-bot/myapp")
		buildRepo := &stubBuildRepo{}
		svc := NewBuildService(imageRepoRepo, buildRepo, nil, nil)

		build, err := svc.CreateBuild(context.Background(), "myapp", CreateBuildRequest{
			GitRef: tt.gitRef,
		})
		if err != nil {
			t.Fatalf("gitRef=%q: unexpected error: %v", tt.gitRef, err)
		}
		if build.Channel != tt.wantChannel {
			t.Errorf("gitRef=%q: Channel = %q, want %q", tt.gitRef, build.Channel, tt.wantChannel)
		}
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
