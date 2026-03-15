package repository

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

// --- CIConfigRepo ---

var _ port.CIConfigRepository = (*CIConfigRepo)(nil)

type CIConfigRepo struct {
	db *gorm.DB
}

func NewCIConfigRepo(db *gorm.DB) *CIConfigRepo {
	return &CIConfigRepo{db: db}
}

func (r *CIConfigRepo) Save(ctx context.Context, cfg *domain.CIConfig) error {
	m := ciConfigToModel(cfg)
	result := r.db.WithContext(ctx).Create(m)
	if result.Error != nil {
		if isUniqueConstraintError(result.Error) {
			return domain.ErrAlreadyExists
		}
		return result.Error
	}
	return nil
}

func (r *CIConfigRepo) FindByID(ctx context.Context, id string) (*domain.CIConfig, error) {
	var m CIConfigModel
	result := r.db.WithContext(ctx).First(&m, "id = ?", id)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrCIConfigNotFound
		}
		return nil, result.Error
	}
	return modelToCIConfig(&m), nil
}

func (r *CIConfigRepo) FindByLane(ctx context.Context, lane string) (*domain.CIConfig, error) {
	var m CIConfigModel
	result := r.db.WithContext(ctx).Where("lane = ? AND status = ?", lane, "active").First(&m)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrCIConfigNotFound
		}
		return nil, result.Error
	}
	return modelToCIConfig(&m), nil
}

func (r *CIConfigRepo) FindByBranch(ctx context.Context, branch string) (*domain.CIConfig, error) {
	var m CIConfigModel
	result := r.db.WithContext(ctx).Where("branch = ? AND status = ?", branch, "active").First(&m)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrCIConfigNotFound
		}
		return nil, result.Error
	}
	return modelToCIConfig(&m), nil
}

func (r *CIConfigRepo) FindActive(ctx context.Context) ([]*domain.CIConfig, error) {
	var models []CIConfigModel
	if err := r.db.WithContext(ctx).Where("status = ?", "active").Find(&models).Error; err != nil {
		return nil, err
	}
	configs := make([]*domain.CIConfig, 0, len(models))
	for i := range models {
		configs = append(configs, modelToCIConfig(&models[i]))
	}
	return configs, nil
}

func (r *CIConfigRepo) Update(ctx context.Context, cfg *domain.CIConfig) error {
	m := ciConfigToModel(cfg)
	return r.db.WithContext(ctx).Save(m).Error
}

func (r *CIConfigRepo) Delete(ctx context.Context, id string) error {
	return r.db.WithContext(ctx).Delete(&CIConfigModel{}, "id = ?", id).Error
}

func ciConfigToModel(c *domain.CIConfig) *CIConfigModel {
	servicesJSON, _ := json.Marshal(c.Services)
	return &CIConfigModel{
		ID:        c.ID,
		Lane:      c.Lane,
		Branch:    c.Branch,
		Services:  string(servicesJSON),
		Status:    c.Status,
		CreatedAt: c.CreatedAt,
		UpdatedAt: c.UpdatedAt,
	}
}

func modelToCIConfig(m *CIConfigModel) *domain.CIConfig {
	var services []string
	if m.Services != "" {
		_ = json.Unmarshal([]byte(m.Services), &services)
	}
	return &domain.CIConfig{
		ID:        m.ID,
		Lane:      m.Lane,
		Branch:    m.Branch,
		Services:  services,
		Status:    m.Status,
		CreatedAt: m.CreatedAt,
		UpdatedAt: m.UpdatedAt,
	}
}

// --- PipelineRunRepo ---

var _ port.PipelineRunRepository = (*PipelineRunRepo)(nil)

type PipelineRunRepo struct {
	db *gorm.DB
}

func NewPipelineRunRepo(db *gorm.DB) *PipelineRunRepo {
	return &PipelineRunRepo{db: db}
}

func (r *PipelineRunRepo) Save(ctx context.Context, run *domain.PipelineRun) error {
	m := pipelineRunToModel(run)
	return r.db.WithContext(ctx).Create(m).Error
}

func (r *PipelineRunRepo) FindByID(ctx context.Context, id string) (*domain.PipelineRun, error) {
	var m PipelineRunModel
	result := r.db.WithContext(ctx).First(&m, "id = ?", id)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrPipelineRunNotFound
		}
		return nil, result.Error
	}
	return modelToPipelineRun(&m), nil
}

func (r *PipelineRunRepo) FindByLane(ctx context.Context, lane string, limit int) ([]*domain.PipelineRun, error) {
	query := r.db.WithContext(ctx).Where("lane = ?", lane).Order("created_at desc")
	if limit > 0 {
		query = query.Limit(limit)
	}
	var models []PipelineRunModel
	if err := query.Find(&models).Error; err != nil {
		return nil, err
	}
	runs := make([]*domain.PipelineRun, 0, len(models))
	for i := range models {
		runs = append(runs, modelToPipelineRun(&models[i]))
	}
	return runs, nil
}

func (r *PipelineRunRepo) ExistsByCommitSHA(ctx context.Context, sha string) (bool, error) {
	var count int64
	err := r.db.WithContext(ctx).Model(&PipelineRunModel{}).Where("commit_sha = ?", sha).Count(&count).Error
	return count > 0, err
}

func (r *PipelineRunRepo) Update(ctx context.Context, run *domain.PipelineRun) error {
	m := pipelineRunToModel(run)
	return r.db.WithContext(ctx).Save(m).Error
}

// --- StageRun CRUD ---

func (r *PipelineRunRepo) SaveStage(ctx context.Context, stage *domain.StageRun) error {
	m := stageRunToModel(stage)
	return r.db.WithContext(ctx).Create(m).Error
}

func (r *PipelineRunRepo) FindStagesByRunID(ctx context.Context, runID string) ([]domain.StageRun, error) {
	var models []StageRunModel
	if err := r.db.WithContext(ctx).Where("pipeline_run_id = ?", runID).Order("seq asc").Find(&models).Error; err != nil {
		return nil, err
	}
	stages := make([]domain.StageRun, 0, len(models))
	for i := range models {
		stages = append(stages, *modelToStageRun(&models[i]))
	}
	return stages, nil
}

func (r *PipelineRunRepo) UpdateStage(ctx context.Context, stage *domain.StageRun) error {
	m := stageRunToModel(stage)
	return r.db.WithContext(ctx).Save(m).Error
}

// --- JobRun CRUD ---

func (r *PipelineRunRepo) SaveJob(ctx context.Context, job *domain.JobRun) error {
	m := jobRunToModel(job)
	return r.db.WithContext(ctx).Create(m).Error
}

func (r *PipelineRunRepo) FindJobsByStageID(ctx context.Context, stageID string) ([]domain.JobRun, error) {
	var models []JobRunModel
	if err := r.db.WithContext(ctx).Where("stage_run_id = ?", stageID).Find(&models).Error; err != nil {
		return nil, err
	}
	jobs := make([]domain.JobRun, 0, len(models))
	for i := range models {
		jobs = append(jobs, *modelToJobRun(&models[i]))
	}
	return jobs, nil
}

func (r *PipelineRunRepo) FindJobByID(ctx context.Context, id string) (*domain.JobRun, error) {
	var m JobRunModel
	result := r.db.WithContext(ctx).First(&m, "id = ?", id)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrPipelineRunNotFound
		}
		return nil, result.Error
	}
	return modelToJobRun(&m), nil
}

func (r *PipelineRunRepo) UpdateJob(ctx context.Context, job *domain.JobRun) error {
	m := jobRunToModel(job)
	return r.db.WithContext(ctx).Save(m).Error
}

// --- Converters ---

func pipelineRunToModel(p *domain.PipelineRun) *PipelineRunModel {
	servicesJSON, _ := json.Marshal(p.Services)
	return &PipelineRunModel{
		ID:         p.ID,
		CIConfigID: p.CIConfigID,
		GitRef:     p.GitRef,
		CommitSHA:  p.CommitSHA,
		Lane:       p.Lane,
		Services:   string(servicesJSON),
		Status:     string(p.Status),
		Message:    p.Message,
		CreatedAt:  p.CreatedAt,
		UpdatedAt:  p.UpdatedAt,
	}
}

func modelToPipelineRun(m *PipelineRunModel) *domain.PipelineRun {
	var services []string
	if m.Services != "" {
		_ = json.Unmarshal([]byte(m.Services), &services)
	}
	return &domain.PipelineRun{
		ID:         m.ID,
		CIConfigID: m.CIConfigID,
		GitRef:     m.GitRef,
		CommitSHA:  m.CommitSHA,
		Lane:       m.Lane,
		Services:   services,
		Status:     domain.PipelineRunStatus(m.Status),
		Message:    m.Message,
		CreatedAt:  m.CreatedAt,
		UpdatedAt:  m.UpdatedAt,
	}
}

func stageRunToModel(s *domain.StageRun) *StageRunModel {
	return &StageRunModel{
		ID:            s.ID,
		PipelineRunID: s.PipelineRunID,
		Stage:         string(s.Stage),
		Seq:           s.Seq,
		Status:        string(s.Status),
		Message:       s.Message,
		CreatedAt:     s.CreatedAt,
		UpdatedAt:     s.UpdatedAt,
	}
}

func modelToStageRun(m *StageRunModel) *domain.StageRun {
	return &domain.StageRun{
		ID:            m.ID,
		PipelineRunID: m.PipelineRunID,
		Stage:         domain.StageType(m.Stage),
		Seq:           m.Seq,
		Status:        domain.PipelineRunStatus(m.Status),
		Message:       m.Message,
		CreatedAt:     m.CreatedAt,
		UpdatedAt:     m.UpdatedAt,
	}
}

func jobRunToModel(j *domain.JobRun) *JobRunModel {
	return &JobRunModel{
		ID:         j.ID,
		StageRunID: j.StageRunID,
		Name:       j.Name,
		JobType:    j.JobType,
		RefID:      j.RefID,
		K8sJobName: j.K8sJobName,
		Status:     string(j.Status),
		Log:        j.Log,
		CreatedAt:  j.CreatedAt,
		UpdatedAt:  j.UpdatedAt,
	}
}

func modelToJobRun(m *JobRunModel) *domain.JobRun {
	return &domain.JobRun{
		ID:         m.ID,
		StageRunID: m.StageRunID,
		Name:       m.Name,
		JobType:    m.JobType,
		RefID:      m.RefID,
		K8sJobName: m.K8sJobName,
		Status:     domain.PipelineRunStatus(m.Status),
		Log:        m.Log,
		CreatedAt:  m.CreatedAt,
		UpdatedAt:  m.UpdatedAt,
	}
}
