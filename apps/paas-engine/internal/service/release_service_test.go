package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// --- stubs for release tests ---

type releaseTestReleaseRepo struct {
	releases map[string]*domain.Release // id -> release
	byApp    map[string]*domain.Release // appName-lane -> release
}

func newReleaseTestReleaseRepo() *releaseTestReleaseRepo {
	return &releaseTestReleaseRepo{
		releases: make(map[string]*domain.Release),
		byApp:    make(map[string]*domain.Release),
	}
}

func (r *releaseTestReleaseRepo) Save(_ context.Context, rel *domain.Release) error {
	r.releases[rel.ID] = rel
	r.byApp[rel.AppName+"-"+rel.Lane] = rel
	return nil
}
func (r *releaseTestReleaseRepo) Update(_ context.Context, rel *domain.Release) error {
	r.releases[rel.ID] = rel
	r.byApp[rel.AppName+"-"+rel.Lane] = rel
	return nil
}
func (r *releaseTestReleaseRepo) Delete(_ context.Context, id string) error {
	delete(r.releases, id)
	return nil
}
func (r *releaseTestReleaseRepo) FindByID(_ context.Context, id string) (*domain.Release, error) {
	if rel, ok := r.releases[id]; ok {
		return rel, nil
	}
	return nil, domain.ErrReleaseNotFound
}
func (r *releaseTestReleaseRepo) FindByAppAndLane(_ context.Context, appName, lane string) (*domain.Release, error) {
	if rel, ok := r.byApp[appName+"-"+lane]; ok {
		return rel, nil
	}
	return nil, domain.ErrReleaseNotFound
}
func (r *releaseTestReleaseRepo) FindAll(_ context.Context, _, _ string) ([]*domain.Release, error) {
	return nil, nil
}

type stubDeployer struct {
	deployErr error
	status    *domain.DeploymentStatus
}

func (s *stubDeployer) Deploy(_ context.Context, _ *domain.Release, _ *domain.App, _ map[string]string) error {
	return s.deployErr
}
func (s *stubDeployer) Delete(_ context.Context, _ *domain.Release, _ bool) error { return nil }
func (s *stubDeployer) GetDeploymentStatus(_ context.Context, name string) (*domain.DeploymentStatus, error) {
	if s.status != nil {
		return s.status, nil
	}
	return &domain.DeploymentStatus{DeployName: name}, nil
}
func (s *stubDeployer) ListManagedResources(_ context.Context) ([]port.ManagedResource, error) {
	return nil, nil
}
func (s *stubDeployer) DeleteResource(_ context.Context, _, _ string) error { return nil }

// --- tests ---

func TestCreateRelease_DeployFailure_SetsMessage(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:          "myapp",
		ImageRepoName: "myapp",
		Port:          8080,
	}}
	imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{
		Name:     "myapp",
		Registry: "harbor.local/inner-bot/myapp",
	}}
	releaseRepo := newReleaseTestReleaseRepo()
	deployer := &stubDeployer{
		deployErr: errors.New("wait for rollout: deployment myapp-prod failed: pod myapp-prod-abc is in CrashLoopBackOff: exit code 1"),
	}

	svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer, nil)

	release, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "myapp",
		Lane:     "prod",
		ImageTag: "abc123",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if release.Status != domain.ReleaseStatusFailed {
		t.Errorf("Status = %q, want %q", release.Status, domain.ReleaseStatusFailed)
	}
	if release.Message == "" {
		t.Error("Message should be non-empty on deploy failure")
	}
	if release.Message != deployer.deployErr.Error() {
		t.Errorf("Message = %q, want %q", release.Message, deployer.deployErr.Error())
	}
}

func TestCreateRelease_DeploySuccess_ClearsMessage(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:          "myapp",
		ImageRepoName: "myapp",
		Port:          8080,
	}}
	imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{
		Name:     "myapp",
		Registry: "harbor.local/inner-bot/myapp",
	}}
	releaseRepo := newReleaseTestReleaseRepo()
	deployer := &stubDeployer{}

	svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer, nil)

	release, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "myapp",
		Lane:     "prod",
		ImageTag: "abc123",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if release.Status != domain.ReleaseStatusDeployed {
		t.Errorf("Status = %q, want %q", release.Status, domain.ReleaseStatusDeployed)
	}
	if release.Message != "" {
		t.Errorf("Message should be empty on success, got %q", release.Message)
	}
}

func TestGetReleaseStatus(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	releaseRepo := newReleaseTestReleaseRepo()
	expectedStatus := &domain.DeploymentStatus{
		DeployName: "myapp-prod",
		Desired:    1,
		Ready:      1,
		Available:  1,
		Pods: []domain.PodStatus{
			{Name: "myapp-prod-abc-1", Status: "Running", Ready: true},
		},
	}
	deployer := &stubDeployer{status: expectedStatus}

	svc := NewReleaseService(appRepo, nil, nil, releaseRepo, deployer, nil)

	// 先存一个 release
	rel := &domain.Release{ID: "r1", AppName: "myapp", Lane: "prod", DeployName: "myapp-prod"}
	_ = releaseRepo.Save(context.Background(), rel)

	status, err := svc.GetReleaseStatus(context.Background(), "r1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if status.DeployName != "myapp-prod" {
		t.Errorf("DeployName = %q, want %q", status.DeployName, "myapp-prod")
	}
	if len(status.Pods) != 1 {
		t.Errorf("Pods count = %d, want 1", len(status.Pods))
	}
}

func TestGetReleaseStatus_NotFound(t *testing.T) {
	releaseRepo := newReleaseTestReleaseRepo()
	deployer := &stubDeployer{}
	svc := NewReleaseService(nil, nil, nil, releaseRepo, deployer, nil)

	_, err := svc.GetReleaseStatus(context.Background(), "nonexistent")
	if !errors.Is(err, domain.ErrReleaseNotFound) {
		t.Errorf("expected ErrReleaseNotFound, got %v", err)
	}
}
