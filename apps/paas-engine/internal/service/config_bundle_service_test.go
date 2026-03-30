package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stub for ConfigBundleRepository ---

type stubConfigBundleRepo struct {
	bundles map[string]*domain.ConfigBundle
}

func newStubConfigBundleRepo() *stubConfigBundleRepo {
	return &stubConfigBundleRepo{bundles: make(map[string]*domain.ConfigBundle)}
}

func (r *stubConfigBundleRepo) Save(_ context.Context, bundle *domain.ConfigBundle) error {
	if _, exists := r.bundles[bundle.Name]; exists {
		return domain.ErrAlreadyExists
	}
	r.bundles[bundle.Name] = bundle
	return nil
}

func (r *stubConfigBundleRepo) FindByName(_ context.Context, name string) (*domain.ConfigBundle, error) {
	b, ok := r.bundles[name]
	if !ok {
		return nil, domain.ErrConfigBundleNotFound
	}
	return b, nil
}

func (r *stubConfigBundleRepo) FindAll(_ context.Context) ([]*domain.ConfigBundle, error) {
	var result []*domain.ConfigBundle
	for _, b := range r.bundles {
		result = append(result, b)
	}
	return result, nil
}

func (r *stubConfigBundleRepo) FindByNames(_ context.Context, names []string) ([]*domain.ConfigBundle, error) {
	var result []*domain.ConfigBundle
	for _, name := range names {
		if b, ok := r.bundles[name]; ok {
			result = append(result, b)
		}
	}
	return result, nil
}

func (r *stubConfigBundleRepo) Update(_ context.Context, bundle *domain.ConfigBundle) error {
	r.bundles[bundle.Name] = bundle
	return nil
}

func (r *stubConfigBundleRepo) Delete(_ context.Context, name string) error {
	delete(r.bundles, name)
	return nil
}

// allAppsStubRepo 是一个支持 FindAll 返回多个 App 的 stub，用于 bundle delete 测试。
type allAppsStubRepo struct {
	apps []*domain.App
}

func (r *allAppsStubRepo) Save(_ context.Context, _ *domain.App) error   { return nil }
func (r *allAppsStubRepo) Update(_ context.Context, _ *domain.App) error { return nil }
func (r *allAppsStubRepo) Delete(_ context.Context, _ string) error      { return nil }
func (r *allAppsStubRepo) FindAll(_ context.Context) ([]*domain.App, error) {
	return r.apps, nil
}
func (r *allAppsStubRepo) FindByName(_ context.Context, name string) (*domain.App, error) {
	for _, a := range r.apps {
		if a.Name == name {
			return a, nil
		}
	}
	return nil, domain.ErrAppNotFound
}

// --- helpers ---

func newConfigBundleService(bundleRepo *stubConfigBundleRepo, appRepo *stubAppRepo, releaseRepo *releaseTestReleaseRepo) *ConfigBundleService {
	return NewConfigBundleService(bundleRepo, appRepo, releaseRepo)
}

// --- tests ---

func TestCreateConfigBundle_Success(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	bundle, err := svc.CreateBundle(context.Background(), CreateBundleRequest{
		Name:        "pg-main",
		Description: "Main PG config",
		Keys:        map[string]string{"PG_HOST": "localhost", "PG_PORT": "5432"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bundle.Name != "pg-main" {
		t.Errorf("Name = %q, want %q", bundle.Name, "pg-main")
	}
	if bundle.Keys["PG_HOST"] != "localhost" {
		t.Errorf("PG_HOST = %q, want %q", bundle.Keys["PG_HOST"], "localhost")
	}
	if bundle.Keys["PG_PORT"] != "5432" {
		t.Errorf("PG_PORT = %q, want %q", bundle.Keys["PG_PORT"], "5432")
	}
}

func TestCreateConfigBundle_InvalidName(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	_, err := svc.CreateBundle(context.Background(), CreateBundleRequest{
		Name: "INVALID_NAME",
	})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestCreateConfigBundle_Duplicate(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	_, err := svc.CreateBundle(context.Background(), CreateBundleRequest{Name: "pg-main"})
	if err != nil {
		t.Fatalf("first create failed: %v", err)
	}

	_, err = svc.CreateBundle(context.Background(), CreateBundleRequest{Name: "pg-main"})
	if !errors.Is(err, domain.ErrAlreadyExists) {
		t.Errorf("expected ErrAlreadyExists, got %v", err)
	}
}

func TestDeleteConfigBundle_ReferencedByApp(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	// Create the bundle first
	if err := bundleRepo.Save(context.Background(), &domain.ConfigBundle{Name: "pg-main"}); err != nil {
		t.Fatalf("setup: %v", err)
	}

	// App that references pg-main — use allAppsStubRepo so FindAll returns the app
	appRepo := &allAppsStubRepo{apps: []*domain.App{
		{Name: "my-app", ConfigBundles: []string{"pg-main"}},
	}}

	svc := NewConfigBundleService(bundleRepo, appRepo, newReleaseTestReleaseRepo())

	if err := svc.DeleteBundle(context.Background(), "pg-main"); !errors.Is(err, domain.ErrCannotDelete) {
		t.Errorf("expected ErrCannotDelete, got %v", err)
	}
}

func TestSetKeys_MergesWithExisting(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_HOST": "localhost"},
	}
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	bundle, err := svc.SetKeys(context.Background(), "pg-main", []byte(`{"PG_PORT":"5432"}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bundle.Keys["PG_HOST"] != "localhost" {
		t.Errorf("PG_HOST should be preserved, got %q", bundle.Keys["PG_HOST"])
	}
	if bundle.Keys["PG_PORT"] != "5432" {
		t.Errorf("PG_PORT = %q, want %q", bundle.Keys["PG_PORT"], "5432")
	}
}

func TestSetKeys_DeleteKeyWithNull(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_HOST": "localhost", "PG_PORT": "5432"},
	}
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	bundle, err := svc.SetKeys(context.Background(), "pg-main", []byte(`{"PG_PORT":null}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, ok := bundle.Keys["PG_PORT"]; ok {
		t.Error("PG_PORT should be deleted")
	}
	if bundle.Keys["PG_HOST"] != "localhost" {
		t.Errorf("PG_HOST should be preserved, got %q", bundle.Keys["PG_HOST"])
	}
}

func TestDeleteKey_AlsoRemovesFromLaneOverrides(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_HOST": "localhost", "PG_PORT": "5432"},
		LaneOverrides: map[string]map[string]string{
			"dev": {"PG_PORT": "5433", "PG_HOST": "dev-db"},
		},
	}
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	bundle, err := svc.DeleteKey(context.Background(), "pg-main", "PG_PORT")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, ok := bundle.Keys["PG_PORT"]; ok {
		t.Error("PG_PORT should be deleted from Keys")
	}
	if devOverrides, ok := bundle.LaneOverrides["dev"]; ok {
		if _, ok := devOverrides["PG_PORT"]; ok {
			t.Error("PG_PORT should be deleted from dev lane override")
		}
	}
	// PG_HOST in override should remain
	if bundle.LaneOverrides["dev"]["PG_HOST"] != "dev-db" {
		t.Errorf("PG_HOST override should remain, got %q", bundle.LaneOverrides["dev"]["PG_HOST"])
	}
}

func TestSetLaneOverrides_Success(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_HOST": "localhost"},
	}
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	bundle, err := svc.SetLaneOverrides(context.Background(), "pg-main", "dev", []byte(`{"PG_HOST":"dev-db"}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bundle.LaneOverrides["dev"]["PG_HOST"] != "dev-db" {
		t.Errorf("dev override PG_HOST = %q, want %q", bundle.LaneOverrides["dev"]["PG_HOST"], "dev-db")
	}
}

func TestGenerateKey_CreatesRandomValue(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{},
	}
	svc := NewConfigBundleService(bundleRepo, &stubAppRepo{}, newReleaseTestReleaseRepo())

	bundle, err := svc.GenerateKey(context.Background(), "pg-main", "SECRET_KEY", 32)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	val, ok := bundle.Keys["SECRET_KEY"]
	if !ok {
		t.Fatal("SECRET_KEY should exist in Keys")
	}
	// 32 bytes hex encoded = 64 chars
	if len(val) != 64 {
		t.Errorf("generated key length = %d, want 64 (hex of 32 bytes)", len(val))
	}
}

func TestResolveConfig_FullMerge(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{
			"PG_HOST": "prod-db",
			"PG_PORT": "5432",
		},
		LaneOverrides: map[string]map[string]string{
			"dev": {"PG_HOST": "dev-db"},
		},
	}

	// stubAppRepo returns a single app when FindByName is called,
	// but FindAll returns nil — good enough for resolve (no ReferencedBy needed here)
	appRepo := &stubAppRepo{
		app: &domain.App{
			Name:          "my-app",
			ConfigBundles: []string{"pg-main"},
			Envs:          map[string]string{"APP_ENV": "dev", "PG_PORT": "5999"}, // overrides bundle
		},
	}

	releaseRepo := newReleaseTestReleaseRepo()
	_ = releaseRepo.Save(context.Background(), &domain.Release{
		ID:      "r1",
		AppName: "my-app",
		Lane:    "dev",
		Envs:    map[string]string{"EXTRA": "from-release"},
		Version: "1.0.0",
	})

	svc := NewConfigBundleService(bundleRepo, appRepo, releaseRepo)

	result, err := svc.ResolveConfig(context.Background(), "my-app", "dev")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Bundle baseline: PG_HOST=prod-db, PG_PORT=5432
	// Bundle dev override: PG_HOST=dev-db  → overrides baseline
	// App envs: APP_ENV=dev, PG_PORT=5999  → overrides bundle
	// Release envs: EXTRA=from-release
	// Auto: LANE=dev, VERSION=1.0.0

	cases := []struct {
		key    string
		value  string
		source string
	}{
		{"PG_HOST", "dev-db", "pg-main[lane:dev]"},
		{"PG_PORT", "5999", "app"},
		{"APP_ENV", "dev", "app"},
		{"EXTRA", "from-release", "release"},
		{"LANE", "dev", "auto"},
		{"VERSION", "1.0.0", "auto"},
	}

	for _, c := range cases {
		entry, ok := result[c.key]
		if !ok {
			t.Errorf("key %q not found in result", c.key)
			continue
		}
		if entry.Value != c.value {
			t.Errorf("result[%q].Value = %q, want %q", c.key, entry.Value, c.value)
		}
		if entry.Source != c.source {
			t.Errorf("result[%q].Source = %q, want %q", c.key, entry.Source, c.source)
		}
	}
}
