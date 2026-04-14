package service

import (
	"context"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stub for DynamicConfigRepository ---

type stubDynamicConfigRepo struct {
	configs map[string]*domain.DynamicConfig // key = "key|lane"
}

func newStubDynamicConfigRepo() *stubDynamicConfigRepo {
	return &stubDynamicConfigRepo{configs: make(map[string]*domain.DynamicConfig)}
}

func compositeKey(key, lane string) string { return key + "|" + lane }

func (r *stubDynamicConfigRepo) Upsert(_ context.Context, config *domain.DynamicConfig) error {
	r.configs[compositeKey(config.Key, config.Lane)] = config
	return nil
}

func (r *stubDynamicConfigRepo) FindByKeyAndLane(_ context.Context, key, lane string) (*domain.DynamicConfig, error) {
	c, ok := r.configs[compositeKey(key, lane)]
	if !ok {
		return nil, domain.ErrDynamicConfigNotFound
	}
	return c, nil
}

func (r *stubDynamicConfigRepo) FindByLane(_ context.Context, lane string) ([]*domain.DynamicConfig, error) {
	var result []*domain.DynamicConfig
	for _, c := range r.configs {
		if c.Lane == lane {
			result = append(result, c)
		}
	}
	return result, nil
}

func (r *stubDynamicConfigRepo) FindAll(_ context.Context) ([]*domain.DynamicConfig, error) {
	var result []*domain.DynamicConfig
	for _, c := range r.configs {
		result = append(result, c)
	}
	return result, nil
}

func (r *stubDynamicConfigRepo) DeleteByKeyAndLane(_ context.Context, key, lane string) error {
	ck := compositeKey(key, lane)
	if _, ok := r.configs[ck]; !ok {
		return domain.ErrDynamicConfigNotFound
	}
	delete(r.configs, ck)
	return nil
}

func (r *stubDynamicConfigRepo) DeleteByKey(_ context.Context, key string) error {
	found := false
	for ck, c := range r.configs {
		if c.Key == key {
			delete(r.configs, ck)
			found = true
		}
	}
	if !found {
		return domain.ErrDynamicConfigNotFound
	}
	return nil
}

// --- tests ---

func TestResolve_ProdBaseline(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	repo.configs[compositeKey("model", "prod")] = &domain.DynamicConfig{
		Key: "model", Lane: "prod", Value: "gemini", UpdatedAt: time.Now(),
	}
	svc := NewDynamicConfigService(repo)

	result, err := svc.Resolve(context.Background(), "prod")
	if err != nil {
		t.Fatal(err)
	}
	if result.Configs["model"].Value != "gemini" {
		t.Errorf("expected gemini, got %s", result.Configs["model"].Value)
	}
	if result.Configs["model"].Lane != "prod" {
		t.Errorf("expected lane=prod, got %s", result.Configs["model"].Lane)
	}
}

func TestResolve_LaneOverride(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	repo.configs[compositeKey("model", "prod")] = &domain.DynamicConfig{
		Key: "model", Lane: "prod", Value: "gemini", UpdatedAt: time.Now(),
	}
	repo.configs[compositeKey("model", "dev")] = &domain.DynamicConfig{
		Key: "model", Lane: "dev", Value: "gpt-4o", UpdatedAt: time.Now(),
	}
	repo.configs[compositeKey("threshold", "prod")] = &domain.DynamicConfig{
		Key: "threshold", Lane: "prod", Value: "0.7", UpdatedAt: time.Now(),
	}
	svc := NewDynamicConfigService(repo)

	result, err := svc.Resolve(context.Background(), "dev")
	if err != nil {
		t.Fatal(err)
	}
	if result.Configs["model"].Value != "gpt-4o" {
		t.Errorf("expected gpt-4o, got %s", result.Configs["model"].Value)
	}
	if result.Configs["model"].Lane != "dev" {
		t.Errorf("expected lane=dev, got %s", result.Configs["model"].Lane)
	}
	if result.Configs["threshold"].Value != "0.7" {
		t.Errorf("expected 0.7, got %s", result.Configs["threshold"].Value)
	}
	if result.Configs["threshold"].Lane != "prod" {
		t.Errorf("expected lane=prod, got %s", result.Configs["threshold"].Lane)
	}
}

func TestResolve_EmptyLaneFallbackProd(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	repo.configs[compositeKey("model", "prod")] = &domain.DynamicConfig{
		Key: "model", Lane: "prod", Value: "gemini", UpdatedAt: time.Now(),
	}
	svc := NewDynamicConfigService(repo)

	result, err := svc.Resolve(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Configs["model"].Value != "gemini" {
		t.Errorf("expected gemini, got %s", result.Configs["model"].Value)
	}
}

func TestSetAndDelete(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	svc := NewDynamicConfigService(repo)

	err := svc.Set(context.Background(), "model", SetDynamicConfigRequest{Lane: "prod", Value: "gemini"})
	if err != nil {
		t.Fatal(err)
	}

	result, err := svc.Resolve(context.Background(), "prod")
	if err != nil {
		t.Fatal(err)
	}
	if result.Configs["model"].Value != "gemini" {
		t.Errorf("expected gemini, got %s", result.Configs["model"].Value)
	}

	err = svc.Delete(context.Background(), "model", "prod")
	if err != nil {
		t.Fatal(err)
	}

	result, err = svc.Resolve(context.Background(), "prod")
	if err != nil {
		t.Fatal(err)
	}
	if _, exists := result.Configs["model"]; exists {
		t.Error("expected model to be deleted")
	}
}
