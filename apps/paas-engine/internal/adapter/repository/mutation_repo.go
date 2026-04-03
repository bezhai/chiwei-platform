package repository

import (
	"context"
	"time"

	"gorm.io/gorm"
)

// MutationRepo 实现对 db_mutations 表的增删改查。
type MutationRepo struct {
	db *gorm.DB
}

func NewMutationRepo(db *gorm.DB) *MutationRepo {
	return &MutationRepo{db: db}
}

func (r *MutationRepo) Create(ctx context.Context, m *DbMutationModel) error {
	return r.db.WithContext(ctx).Create(m).Error
}

// List 返回 db_mutations 记录，按 created_at 降序。status 为空时返回全部。
func (r *MutationRepo) List(ctx context.Context, status string) ([]DbMutationModel, error) {
	q := r.db.WithContext(ctx).Order("created_at DESC")
	if status != "" {
		q = q.Where("status = ?", status)
	}
	var result []DbMutationModel
	return result, q.Find(&result).Error
}

func (r *MutationRepo) Get(ctx context.Context, id uint) (*DbMutationModel, error) {
	var m DbMutationModel
	if err := r.db.WithContext(ctx).First(&m, id).Error; err != nil {
		return nil, err
	}
	return &m, nil
}

// UpdateStatus 更新审批结果相关字段。
func (r *MutationRepo) UpdateStatus(ctx context.Context, id uint, status, reviewedBy, reviewNote string, executedAt *time.Time, execErr string) error {
	updates := map[string]any{
		"status":      status,
		"reviewed_by": reviewedBy,
		"review_note": reviewNote,
		"executed_at": executedAt,
		"error":       execErr,
	}
	return r.db.WithContext(ctx).Model(&DbMutationModel{}).Where("id = ?", id).Updates(updates).Error
}
