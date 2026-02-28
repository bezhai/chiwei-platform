package service

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"github.com/google/uuid"
)

type ReleaseService struct {
	appRepo       port.AppRepository
	imageRepoRepo port.ImageRepoRepository
	laneRepo      port.LaneRepository
	releaseRepo   port.ReleaseRepository
	deployer      port.Deployer
}

func NewReleaseService(
	appRepo port.AppRepository,
	imageRepoRepo port.ImageRepoRepository,
	laneRepo port.LaneRepository,
	releaseRepo port.ReleaseRepository,
	deployer port.Deployer,
) *ReleaseService {
	return &ReleaseService{
		appRepo:       appRepo,
		imageRepoRepo: imageRepoRepo,
		laneRepo:      laneRepo,
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
	if _, err := s.laneRepo.FindByName(ctx, lane); err != nil {
		return nil, err
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
		if err := s.deployer.Delete(ctx, release); err != nil {
			slog.Warn("failed to delete K8s resources", "release_id", release.ID, "error", err)
		}
	}

	if err := s.releaseRepo.Delete(ctx, release.ID); err != nil {
		return err
	}

	return nil
}
