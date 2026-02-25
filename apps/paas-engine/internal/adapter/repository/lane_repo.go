package repository

import (
	"context"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

var _ port.LaneRepository = (*LaneRepo)(nil)

type LaneRepo struct {
	db *gorm.DB
}

func NewLaneRepo(db *gorm.DB) *LaneRepo {
	return &LaneRepo{db: db}
}

func (r *LaneRepo) Save(ctx context.Context, lane *domain.Lane) error {
	m := laneToModel(lane)
	result := r.db.WithContext(ctx).Create(m)
	if result.Error != nil {
		if isUniqueConstraintError(result.Error) {
			return domain.ErrAlreadyExists
		}
		return result.Error
	}
	return nil
}

func (r *LaneRepo) FindByName(ctx context.Context, name string) (*domain.Lane, error) {
	var m LaneModel
	result := r.db.WithContext(ctx).First(&m, "name = ?", name)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrLaneNotFound
		}
		return nil, result.Error
	}
	return modelToLane(&m), nil
}

func (r *LaneRepo) FindAll(ctx context.Context) ([]*domain.Lane, error) {
	var models []LaneModel
	if err := r.db.WithContext(ctx).Find(&models).Error; err != nil {
		return nil, err
	}
	lanes := make([]*domain.Lane, 0, len(models))
	for i := range models {
		lanes = append(lanes, modelToLane(&models[i]))
	}
	return lanes, nil
}

func (r *LaneRepo) Delete(ctx context.Context, name string) error {
	return r.db.WithContext(ctx).Delete(&LaneModel{}, "name = ?", name).Error
}

func laneToModel(l *domain.Lane) *LaneModel {
	return &LaneModel{
		Name:        l.Name,
		Description: l.Description,
		CreatedAt:   l.CreatedAt,
		UpdatedAt:   l.UpdatedAt,
	}
}

func modelToLane(m *LaneModel) *domain.Lane {
	return &domain.Lane{
		Name:        m.Name,
		Description: m.Description,
		CreatedAt:   m.CreatedAt,
		UpdatedAt:   m.UpdatedAt,
	}
}

