package service

import (
	"context"
	"errors"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type LaneService struct {
	laneRepo    port.LaneRepository
	releaseRepo port.ReleaseRepository
}

func NewLaneService(laneRepo port.LaneRepository, releaseRepo port.ReleaseRepository) *LaneService {
	return &LaneService{laneRepo: laneRepo, releaseRepo: releaseRepo}
}

// EnsureDefaultLane 在系统启动时创建 prod 泳道（幂等）。
func (s *LaneService) EnsureDefaultLane(ctx context.Context) error {
	_, err := s.laneRepo.FindByName(ctx, domain.DefaultLane)
	if err == nil {
		return nil // 已存在
	}
	if !errors.Is(err, domain.ErrLaneNotFound) {
		return err
	}
	now := time.Now()
	return s.laneRepo.Save(ctx, &domain.Lane{
		Name:        domain.DefaultLane,
		Description: "Default production lane",
		CreatedAt:   now,
		UpdatedAt:   now,
	})
}

type CreateLaneRequest struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

func (s *LaneService) CreateLane(ctx context.Context, req CreateLaneRequest) (*domain.Lane, error) {
	if err := domain.ValidateK8sName(req.Name); err != nil {
		return nil, err
	}
	now := time.Now()
	lane := &domain.Lane{
		Name:        req.Name,
		Description: req.Description,
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := s.laneRepo.Save(ctx, lane); err != nil {
		return nil, err
	}
	return lane, nil
}

func (s *LaneService) GetLane(ctx context.Context, name string) (*domain.Lane, error) {
	return s.laneRepo.FindByName(ctx, name)
}

func (s *LaneService) ListLanes(ctx context.Context) ([]*domain.Lane, error) {
	return s.laneRepo.FindAll(ctx)
}

func (s *LaneService) DeleteLane(ctx context.Context, name string) error {
	lane, err := s.laneRepo.FindByName(ctx, name)
	if err != nil {
		return err
	}
	if lane.IsDefault() {
		return domain.ErrCannotDelete
	}
	// 检查是否还有关联的 Release
	releases, err := s.releaseRepo.FindByLane(ctx, name)
	if err != nil {
		return err
	}
	if len(releases) > 0 {
		return domain.ErrCannotDelete
	}
	return s.laneRepo.Delete(ctx, name)
}
