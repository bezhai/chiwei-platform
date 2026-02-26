package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stubs for image repo tests ---

type stubImageRepoRepo struct {
	repo *domain.ImageRepo
	err  error
}

func (s *stubImageRepoRepo) Save(_ context.Context, _ *domain.ImageRepo) error { return s.err }
func (s *stubImageRepoRepo) Update(_ context.Context, _ *domain.ImageRepo) error {
	return s.err
}
func (s *stubImageRepoRepo) Delete(_ context.Context, _ string) error { return s.err }
func (s *stubImageRepoRepo) FindAll(_ context.Context) ([]*domain.ImageRepo, error) {
	if s.repo != nil {
		return []*domain.ImageRepo{s.repo}, nil
	}
	return nil, nil
}
func (s *stubImageRepoRepo) FindByName(_ context.Context, _ string) (*domain.ImageRepo, error) {
	if s.err != nil {
		return nil, s.err
	}
	return s.repo, nil
}

// --- tests ---

func TestCreateImageRepo_Success(t *testing.T) {
	svc := NewImageRepoService(&stubImageRepoRepo{}, &stubAppRepo{})

	repo, err := svc.CreateImageRepo(context.Background(), CreateImageRepoRequest{
		Name:       "agent-service",
		Registry:   "harbor.local/inner-bot/agent-service",
		GitRepo:    "https://github.com/bezhai/chiwei-platform.git",
		ContextDir: "apps/agent-service",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if repo.Name != "agent-service" {
		t.Errorf("Name = %q, want %q", repo.Name, "agent-service")
	}
	if repo.Registry != "harbor.local/inner-bot/agent-service" {
		t.Errorf("Registry = %q, want %q", repo.Registry, "harbor.local/inner-bot/agent-service")
	}
}

func TestCreateImageRepo_InvalidName(t *testing.T) {
	svc := NewImageRepoService(&stubImageRepoRepo{}, &stubAppRepo{})

	_, err := svc.CreateImageRepo(context.Background(), CreateImageRepoRequest{
		Name:     "INVALID_NAME",
		Registry: "harbor.local/inner-bot/test",
		GitRepo:  "https://github.com/example/repo.git",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestCreateImageRepo_MissingRegistry(t *testing.T) {
	svc := NewImageRepoService(&stubImageRepoRepo{}, &stubAppRepo{})

	_, err := svc.CreateImageRepo(context.Background(), CreateImageRepoRequest{
		Name:    "myrepo",
		GitRepo: "https://github.com/example/repo.git",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestCreateImageRepo_InvalidContextDir(t *testing.T) {
	svc := NewImageRepoService(&stubImageRepoRepo{}, &stubAppRepo{})

	_, err := svc.CreateImageRepo(context.Background(), CreateImageRepoRequest{
		Name:       "myrepo",
		Registry:   "harbor.local/inner-bot/test",
		GitRepo:    "https://github.com/example/repo.git",
		ContextDir: "../etc/passwd",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestCreateImageRepo_InvalidGitRepo(t *testing.T) {
	svc := NewImageRepoService(&stubImageRepoRepo{}, &stubAppRepo{})

	_, err := svc.CreateImageRepo(context.Background(), CreateImageRepoRequest{
		Name:     "myrepo",
		Registry: "harbor.local/inner-bot/test",
		GitRepo:  "ftp://invalid.com/repo.git",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestImageRepo_FullImageRef(t *testing.T) {
	repo := &domain.ImageRepo{
		Registry: "harbor.local/inner-bot/agent-service",
	}
	got := repo.FullImageRef("abc123")
	want := "harbor.local/inner-bot/agent-service:abc123"
	if got != want {
		t.Errorf("FullImageRef() = %q, want %q", got, want)
	}
}
