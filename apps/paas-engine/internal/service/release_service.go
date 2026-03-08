package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/metrics"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"github.com/google/uuid"
)

type ReleaseService struct {
	appRepo       port.AppRepository
	imageRepoRepo port.ImageRepoRepository
	releaseRepo   port.ReleaseRepository
	deployer      port.Deployer
}

func NewReleaseService(
	appRepo port.AppRepository,
	imageRepoRepo port.ImageRepoRepository,
	releaseRepo port.ReleaseRepository,
	deployer port.Deployer,
) *ReleaseService {
	return &ReleaseService{
		appRepo:       appRepo,
		imageRepoRepo: imageRepoRepo,
		releaseRepo:   releaseRepo,
		deployer:      deployer,
	}
}

type CreateReleaseRequest struct {
	AppName  string            `json:"app_name"`
	Lane     string            `json:"lane"`
	ImageTag string            `json:"image_tag"` // tag 部分，完整 URL 由 App → ImageRepo 拼出
	Replicas int32             `json:"replicas"`
	Envs     map[string]string `json:"envs"`
	Version  string            `json:"version"` // 自定义版本标识，可选
}

func (s *ReleaseService) CreateOrUpdateRelease(ctx context.Context, req CreateReleaseRequest) (*domain.Release, error) {
	app, err := s.appRepo.FindByName(ctx, req.AppName)
	if err != nil {
		return nil, err
	}

	// 通过 App → ImageRepo 拼完整镜像地址
	var fullImage string
	if app.ImageRepoName != "" {
		imageRepo, err := s.imageRepoRepo.FindByName(ctx, app.ImageRepoName)
		if err != nil {
			return nil, err
		}
		fullImage = imageRepo.FullImageRef(req.ImageTag)
	}

	lane := req.Lane
	if lane == "" {
		lane = domain.DefaultLane
	}

	if req.Replicas <= 0 {
		req.Replicas = 1
	}

	existing, err := s.releaseRepo.FindByAppAndLane(ctx, req.AppName, lane)
	if err != nil && !errors.Is(err, domain.ErrReleaseNotFound) {
		return nil, err
	}

	now := time.Now()
	var release *domain.Release

	if existing != nil {
		existing.Image = fullImage
		existing.Replicas = req.Replicas
		existing.Envs = req.Envs
		existing.Version = req.Version
		existing.Status = domain.ReleaseStatusPending
		existing.UpdatedAt = now
		release = existing
	} else {
		release = &domain.Release{
			ID:        uuid.New().String(),
			AppName:   req.AppName,
			Lane:      lane,
			Image:     fullImage,
			Replicas:  req.Replicas,
			Envs:      req.Envs,
			Version:   req.Version,
			Status:    domain.ReleaseStatusPending,
			CreatedAt: now,
			UpdatedAt: now,
		}
	}
	release.DeployName = release.ResourceName()

	// 下发 K8s 资源
	if s.deployer != nil {
		if err := s.deployer.Deploy(ctx, release, app); err != nil {
			release.Status = domain.ReleaseStatusFailed
		} else {
			release.Status = domain.ReleaseStatusDeployed
		}
	} else {
		release.Status = domain.ReleaseStatusDeployed
	}

	if existing != nil {
		if err := s.releaseRepo.Update(ctx, release); err != nil {
			return nil, err
		}
	} else {
		if err := s.releaseRepo.Save(ctx, release); err != nil {
			return nil, err
		}
	}

	if release.Status == domain.ReleaseStatusDeployed {
		metrics.ReleasesTotal.WithLabelValues(release.Lane).Inc()
	}

	return release, nil
}

func (s *ReleaseService) GetRelease(ctx context.Context, id string) (*domain.Release, error) {
	return s.releaseRepo.FindByID(ctx, id)
}

func (s *ReleaseService) ListReleases(ctx context.Context, appName, lane string) ([]*domain.Release, error) {
	return s.releaseRepo.FindAll(ctx, appName, lane)
}

func (s *ReleaseService) UpdateRelease(ctx context.Context, id string, req CreateReleaseRequest) (*domain.Release, error) {
	release, err := s.releaseRepo.FindByID(ctx, id)
	if err != nil {
		return nil, err
	}
	req.AppName = release.AppName
	req.Lane = release.Lane
	return s.CreateOrUpdateRelease(ctx, req)
}

func (s *ReleaseService) DeleteReleaseByAppAndLane(ctx context.Context, appName, lane string) error {
	release, err := s.releaseRepo.FindByAppAndLane(ctx, appName, lane)
	if err != nil {
		return err
	}
	return s.deleteRelease(ctx, release)
}

func (s *ReleaseService) DeleteRelease(ctx context.Context, id string) error {
	release, err := s.releaseRepo.FindByID(ctx, id)
	if err != nil {
		return err
	}
	return s.deleteRelease(ctx, release)
}

func (s *ReleaseService) deleteRelease(ctx context.Context, release *domain.Release) error {
	if s.deployer != nil {
		// 查询该 app 是否还有其他 release
		others, err := s.releaseRepo.FindAll(ctx, release.AppName, "")
		if err != nil {
			return err
		}
		hasOthers := false
		for _, r := range others {
			if r.ID != release.ID {
				hasOthers = true
				break
			}
		}
		// K8s 删除失败直接返回错误，不删 DB
		if err := s.deployer.Delete(ctx, release, hasOthers); err != nil {
			return fmt.Errorf("delete k8s resources: %w", err)
		}
	}

	return s.releaseRepo.Delete(ctx, release.ID)
}

// OrphanReport 包含 K8s 和 DB 中的孤儿资源。
type OrphanReport struct {
	K8sOrphans []port.ManagedResource `json:"k8s_orphans"` // K8s 存在但 DB 无记录
	DBOrphans  []*domain.Release      `json:"db_orphans"`  // DB 存在但 K8s 无对应资源
}

// DetectOrphans 对比 K8s 资源和 DB release 记录，返回孤儿资源。
func (s *ReleaseService) DetectOrphans(ctx context.Context) (*OrphanReport, error) {
	if s.deployer == nil {
		return &OrphanReport{}, nil
	}

	k8sResources, err := s.deployer.ListManagedResources(ctx)
	if err != nil {
		return nil, fmt.Errorf("list managed resources: %w", err)
	}

	dbReleases, err := s.releaseRepo.FindAll(ctx, "", "")
	if err != nil {
		return nil, fmt.Errorf("list releases: %w", err)
	}

	// 构建 DB release 索引: resourceName -> release, appName -> true
	dbResourceNames := make(map[string]bool)
	dbAppNames := make(map[string]bool)
	for _, r := range dbReleases {
		dbResourceNames[r.ResourceName()] = true
		dbAppNames[r.AppName] = true
	}

	// K8s 孤儿: K8s 中存在但 DB 无对应 release
	var k8sOrphans []port.ManagedResource
	for _, res := range k8sResources {
		if res.Lane != "" {
			// lane resource: 对应 {app}-{lane}
			resourceName := res.AppName + "-" + res.Lane
			if !dbResourceNames[resourceName] {
				k8sOrphans = append(k8sOrphans, res)
			}
		} else {
			// base service (无 lane): 只要该 app 在 DB 中还有任何 release 就不算孤儿
			if !dbAppNames[res.AppName] {
				k8sOrphans = append(k8sOrphans, res)
			}
		}
	}

	// DB 孤儿: DB 中存在但 K8s 无对应 Deployment
	k8sDeployments := make(map[string]bool)
	for _, res := range k8sResources {
		if res.Kind == "Deployment" {
			k8sDeployments[res.Name] = true
		}
	}
	var dbOrphans []*domain.Release
	for _, r := range dbReleases {
		if !k8sDeployments[r.ResourceName()] {
			dbOrphans = append(dbOrphans, r)
		}
	}

	return &OrphanReport{
		K8sOrphans: k8sOrphans,
		DBOrphans:  dbOrphans,
	}, nil
}

// CleanupOrphans 删除所有 K8s 孤儿资源。
func (s *ReleaseService) CleanupOrphans(ctx context.Context) (*OrphanReport, error) {
	report, err := s.DetectOrphans(ctx)
	if err != nil {
		return nil, err
	}

	for _, res := range report.K8sOrphans {
		if err := s.deployer.DeleteResource(ctx, res.Kind, res.Name); err != nil {
			return nil, fmt.Errorf("delete orphan %s %s: %w", res.Kind, res.Name, err)
		}
	}

	return report, nil
}
