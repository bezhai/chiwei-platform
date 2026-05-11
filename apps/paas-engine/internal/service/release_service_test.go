package service

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// newReleaseSvcWithBundles 构造带 configBundleSvc 的 ReleaseService，供 RequiredKeys 校验测试使用。
func newReleaseSvcWithBundles(t *testing.T, apps []*domain.App, bundles []*domain.ConfigBundle) *ReleaseService {
	t.Helper()

	appRepo := &multiAppRepo{byName: make(map[string]*domain.App)}
	for _, a := range apps {
		appRepo.byName[a.Name] = a
	}

	bundleRepo := newStubConfigBundleRepo()
	for _, b := range bundles {
		_ = bundleRepo.Save(context.Background(), b)
	}

	configBundleSvc := NewConfigBundleService(bundleRepo, appRepo, newReleaseTestReleaseRepo(), ConfigBundleServiceConfig{})

	return NewReleaseService(
		appRepo,
		&stubImageRepoRepo{repo: &domain.ImageRepo{Name: "agent-service", Registry: "harbor.local/inner-bot/agent-service"}},
		&stubBuildRepo{},
		newReleaseTestReleaseRepo(),
		&stubDeployer{},
		configBundleSvc,
		ReleaseServiceConfig{},
	)
}

// multiAppRepo is a stub AppRepository that returns apps by name from a map.
type multiAppRepo struct {
	byName map[string]*domain.App
}

func (r *multiAppRepo) Save(_ context.Context, app *domain.App) error {
	r.byName[app.Name] = app
	return nil
}
func (r *multiAppRepo) Update(_ context.Context, app *domain.App) error {
	r.byName[app.Name] = app
	return nil
}
func (r *multiAppRepo) Delete(_ context.Context, name string) error {
	delete(r.byName, name)
	return nil
}
func (r *multiAppRepo) FindAll(_ context.Context) ([]*domain.App, error) {
	result := make([]*domain.App, 0, len(r.byName))
	for _, a := range r.byName {
		result = append(result, a)
	}
	return result, nil
}
func (r *multiAppRepo) FindByName(_ context.Context, name string) (*domain.App, error) {
	if a, ok := r.byName[name]; ok {
		return a, nil
	}
	return nil, domain.ErrNotFound
}

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

	svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer, nil, ReleaseServiceConfig{})

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

	svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer, nil, ReleaseServiceConfig{})

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

	svc := NewReleaseService(appRepo, nil, nil, releaseRepo, deployer, nil, ReleaseServiceConfig{})

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

func TestCreateOrUpdateRelease_RejectsBadLaneName(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{
		Name:          "agent-service",
		ImageRepoName: "agent-service",
		Port:          8080,
	}}
	imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{
		Name:     "agent-service",
		Registry: "harbor.local:30002/inner-bot/agent-service",
	}}
	releaseRepo := newReleaseTestReleaseRepo()
	deployer := &stubDeployer{}

	svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer, nil, ReleaseServiceConfig{})

	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "agent-service",
		Lane:     "feature-x", // 无前缀，应 reject
		ImageTag: "1.0.0.1",
	})

	if err == nil {
		t.Fatal("expected lane validation error, got nil")
	}
	if !strings.Contains(err.Error(), "lane") {
		t.Fatalf("error should mention 'lane', got: %v", err)
	}
}

func TestCreateOrUpdateRelease_AcceptsValidLanes(t *testing.T) {
	cases := []string{"prod", "blue", "coe-test-1", "ppe-canary"}
	for _, lane := range cases {
		t.Run(lane, func(t *testing.T) {
			appRepo := &stubAppRepo{app: &domain.App{
				Name:          "agent-service",
				ImageRepoName: "agent-service",
				Port:          8080,
			}}
			imageRepoRepo := &stubImageRepoRepo{repo: &domain.ImageRepo{
				Name:     "agent-service",
				Registry: "harbor.local:30002/inner-bot/agent-service",
			}}
			releaseRepo := newReleaseTestReleaseRepo()
			deployer := &stubDeployer{}

			svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer, nil, ReleaseServiceConfig{})

			_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
				AppName:  "agent-service",
				Lane:     lane,
				ImageTag: "1.0.0.1",
			})
			if err != nil && strings.Contains(err.Error(), "lane") {
				t.Fatalf("lane %q should pass lane validation but got: %v", lane, err)
			}
		})
	}
}

func TestGetReleaseStatus_NotFound(t *testing.T) {
	releaseRepo := newReleaseTestReleaseRepo()
	deployer := &stubDeployer{}
	svc := NewReleaseService(nil, nil, nil, releaseRepo, deployer, nil, ReleaseServiceConfig{})

	_, err := svc.GetReleaseStatus(context.Background(), "nonexistent")
	if !errors.Is(err, domain.ErrReleaseNotFound) {
		t.Errorf("expected ErrReleaseNotFound, got %v", err)
	}
}

func TestCreateOrUpdateRelease_RejectsCoeWithMissingRequiredKey(t *testing.T) {
	bundle := &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres", "POSTGRES_DB": "chiwei"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "test-pg"}, // POSTGRES_DB 漏了
		},
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST", "POSTGRES_DB"}},
	}
	app := &domain.App{Name: "agent-service", ImageRepoName: "agent-service", Port: 8000, ConfigBundles: []string{"pg-main"}}
	svc := newReleaseSvcWithBundles(t, []*domain.App{app}, []*domain.ConfigBundle{bundle})

	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "agent-service",
		Lane:     "coe-foo",
		ImageTag: "1.0.0",
	})
	if err == nil {
		t.Fatal("expected reject for missing RequiredKey")
	}
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("error must wrap ErrInvalidInput: %v", err)
	}
}

func TestCreateOrUpdateRelease_AllowsProdEvenWithCoeRequiredKeys(t *testing.T) {
	// prod lane 不触发 coe RequiredKeys 校验
	bundle := &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres"},
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST", "POSTGRES_DB"}},
	}
	app := &domain.App{Name: "agent-service", ImageRepoName: "agent-service", Port: 8000, ConfigBundles: []string{"pg-main"}}
	svc := newReleaseSvcWithBundles(t, []*domain.App{app}, []*domain.ConfigBundle{bundle})

	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "agent-service",
		Lane:     "prod",
		ImageTag: "1.0.0",
	})
	// prod 泳道不应该触发 coe RequiredKeys 校验
	if err != nil && (strings.Contains(err.Error(), "RequiredKeys") || strings.Contains(err.Error(), "ClassOverrides")) {
		t.Fatalf("prod lane should not trigger coe RequiredKeys check: %v", err)
	}
}
