package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// ResolvedConfigEntry 表示一个已解析的配置项，带来源标注。
type ResolvedConfigEntry struct {
	Value  string `json:"value"`
	Source string `json:"source"`
}

// ConfigBundleService 提供 ConfigBundle 的 CRUD、Key 管理、泳道覆盖和配置解析。
type ConfigBundleService struct {
	bundleRepo  port.ConfigBundleRepository
	appRepo     port.AppRepository
	releaseRepo port.ReleaseRepository
}

// NewConfigBundleService 创建 ConfigBundleService 实例。
func NewConfigBundleService(
	bundleRepo port.ConfigBundleRepository,
	appRepo port.AppRepository,
	releaseRepo port.ReleaseRepository,
) *ConfigBundleService {
	return &ConfigBundleService{
		bundleRepo:  bundleRepo,
		appRepo:     appRepo,
		releaseRepo: releaseRepo,
	}
}

// CreateBundleRequest 创建 ConfigBundle 的请求体。
type CreateBundleRequest struct {
	Name        string            `json:"name"`
	Description string            `json:"description"`
	Keys        map[string]string `json:"keys"`
}

// CreateBundle 创建一个新的 ConfigBundle。
func (s *ConfigBundleService) CreateBundle(ctx context.Context, req CreateBundleRequest) (*domain.ConfigBundle, error) {
	if err := domain.ValidateK8sName(req.Name); err != nil {
		return nil, err
	}
	now := time.Now()
	bundle := &domain.ConfigBundle{
		Name:        req.Name,
		Description: req.Description,
		Keys:        req.Keys,
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := s.bundleRepo.Save(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// GetBundle 查找一个 ConfigBundle，并填充 ReferencedBy（扫描所有 App）。
func (s *ConfigBundleService) GetBundle(ctx context.Context, name string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}

	// 扫描所有 app，找到引用此 bundle 的 app 列表
	apps, err := s.appRepo.FindAll(ctx)
	if err != nil {
		return nil, err
	}
	var referencedBy []string
	for _, app := range apps {
		for _, b := range app.ConfigBundles {
			if b == name {
				referencedBy = append(referencedBy, app.Name)
				break
			}
		}
	}
	bundle.ReferencedBy = referencedBy
	return bundle, nil
}

// ListBundles 返回所有 ConfigBundle。
func (s *ConfigBundleService) ListBundles(ctx context.Context) ([]*domain.ConfigBundle, error) {
	return s.bundleRepo.FindAll(ctx)
}

// UpdateBundle 对 ConfigBundle 的字段做部分更新，keys 使用 MergeEnvs 语义。
func (s *ConfigBundleService) UpdateBundle(ctx context.Context, name string, body []byte) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}

	fields, err := ParseFields(body)
	if err != nil {
		return nil, domain.ErrInvalidInput
	}

	if err := ApplyField(fields, "description", &bundle.Description); err != nil {
		return nil, domain.ErrInvalidInput
	}

	bundle.Keys, err = MergeEnvs(bundle.Keys, fields["keys"])
	if err != nil {
		return nil, domain.ErrInvalidInput
	}

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// DeleteBundle 删除一个 ConfigBundle，如果有 App 引用则拒绝。
func (s *ConfigBundleService) DeleteBundle(ctx context.Context, name string) error {
	if _, err := s.bundleRepo.FindByName(ctx, name); err != nil {
		return err
	}

	// 检查是否有 App 引用此 bundle
	apps, err := s.appRepo.FindAll(ctx)
	if err != nil {
		return err
	}
	for _, app := range apps {
		for _, b := range app.ConfigBundles {
			if b == name {
				return domain.ErrCannotDelete
			}
		}
	}

	return s.bundleRepo.Delete(ctx, name)
}

// SetKeys 合并更新 bundle 的 Keys（MergeEnvs 语义）。
// body 是一个 JSON 对象，直接作为 keys 的 patch（{"KEY":"val"} 或 {"KEY":null} 删除）。
func (s *ConfigBundleService) SetKeys(ctx context.Context, bundleName string, body []byte) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}

	bundle.Keys, err = MergeEnvs(bundle.Keys, body)
	if err != nil {
		return nil, domain.ErrInvalidInput
	}

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// DeleteKey 从 Keys 中删除一个 key，同时清理所有 LaneOverrides 中该 key。
func (s *ConfigBundleService) DeleteKey(ctx context.Context, bundleName, keyName string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}

	delete(bundle.Keys, keyName)

	// 清理所有 LaneOverrides 中的该 key
	for lane, overrides := range bundle.LaneOverrides {
		delete(overrides, keyName)
		if len(overrides) == 0 {
			delete(bundle.LaneOverrides, lane)
		}
	}

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// GenerateKey 生成随机 hex 编码的 key，length 为字节数（<=0 则默认 32）。
func (s *ConfigBundleService) GenerateKey(ctx context.Context, bundleName, keyName string, length int) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}

	if length <= 0 {
		length = 32
	}

	buf := make([]byte, length)
	if _, err := rand.Read(buf); err != nil {
		return nil, err
	}
	value := hex.EncodeToString(buf)

	if bundle.Keys == nil {
		bundle.Keys = make(map[string]string)
	}
	bundle.Keys[keyName] = value

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// SetLaneOverrides 合并更新某个泳道的覆盖值（MergeEnvs 语义）。
func (s *ConfigBundleService) SetLaneOverrides(ctx context.Context, bundleName, lane string, body []byte) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}

	if bundle.LaneOverrides == nil {
		bundle.LaneOverrides = make(map[string]map[string]string)
	}

	existing := bundle.LaneOverrides[lane]
	merged, err := MergeEnvs(existing, body)
	if err != nil {
		return nil, domain.ErrInvalidInput
	}
	bundle.LaneOverrides[lane] = merged

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// DeleteLaneOverrides 删除某个泳道的全部覆盖值。
func (s *ConfigBundleService) DeleteLaneOverrides(ctx context.Context, bundleName, lane string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}

	delete(bundle.LaneOverrides, lane)

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// DeleteLaneOverrideKey 删除某个泳道覆盖中的单个 key。
func (s *ConfigBundleService) DeleteLaneOverrideKey(ctx context.Context, bundleName, lane, keyName string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}

	if overrides, ok := bundle.LaneOverrides[lane]; ok {
		delete(overrides, keyName)
		if len(overrides) == 0 {
			delete(bundle.LaneOverrides, lane)
		}
	}

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// ResolveConfig 解析 App 在指定泳道的完整配置，按层次合并并标注来源。
// 优先级（低→高）：bundle baseline → bundle lane override → app.Envs → release.Envs → auto-injected
func (s *ConfigBundleService) ResolveConfig(ctx context.Context, appName, lane string) (map[string]ResolvedConfigEntry, error) {
	app, err := s.appRepo.FindByName(ctx, appName)
	if err != nil {
		return nil, err
	}

	result := make(map[string]ResolvedConfigEntry)

	// 1. Bundle baseline + lane override
	if len(app.ConfigBundles) > 0 {
		bundles, err := s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
		if err != nil {
			return nil, err
		}
		for _, bundle := range bundles {
			// baseline
			for k, v := range bundle.Keys {
				result[k] = ResolvedConfigEntry{Value: v, Source: bundle.Name}
			}
			// lane override
			if overrides, ok := bundle.LaneOverrides[lane]; ok {
				for k, v := range overrides {
					result[k] = ResolvedConfigEntry{Value: v, Source: bundle.Name + "[lane:" + lane + "]"}
				}
			}
		}
	}

	// 2. App.Envs
	for k, v := range app.Envs {
		result[k] = ResolvedConfigEntry{Value: v, Source: "app"}
	}

	// 3. Release.Envs
	if lane != "" {
		release, err := s.releaseRepo.FindByAppAndLane(ctx, appName, lane)
		if err != nil && !errors.Is(err, domain.ErrReleaseNotFound) {
			return nil, err
		}
		if release != nil {
			for k, v := range release.Envs {
				result[k] = ResolvedConfigEntry{Value: v, Source: "release"}
			}
			// 4. Auto-injected
			result["LANE"] = ResolvedConfigEntry{Value: lane, Source: "auto"}
			if release.Version != "" {
				result["VERSION"] = ResolvedConfigEntry{Value: release.Version, Source: "auto"}
			}
		}
	}

	return result, nil
}

// ResolveBundleEnvs 仅解析 bundle 层（baseline + lane override），供 deployer 使用。
// 如果 App 没有 ConfigBundles，返回 nil。
func (s *ConfigBundleService) ResolveBundleEnvs(ctx context.Context, app *domain.App, lane string) (map[string]string, error) {
	if len(app.ConfigBundles) == 0 {
		return nil, nil
	}

	bundles, err := s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
	if err != nil {
		return nil, err
	}

	result := make(map[string]string)
	for _, bundle := range bundles {
		for k, v := range bundle.Keys {
			result[k] = v
		}
		if overrides, ok := bundle.LaneOverrides[lane]; ok {
			for k, v := range overrides {
				result[k] = v
			}
		}
	}
	return result, nil
}
