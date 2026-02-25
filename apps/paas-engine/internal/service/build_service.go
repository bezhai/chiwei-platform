package service

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"github.com/google/uuid"
)

type BuildService struct {
	appRepo    port.AppRepository
	buildRepo  port.BuildRepository
	executor   port.BuildExecutor
	logQuerier port.LogQuerier
}

func NewBuildService(
	appRepo port.AppRepository,
	buildRepo port.BuildRepository,
	executor port.BuildExecutor,
	logQuerier port.LogQuerier,
) *BuildService {
	return &BuildService{
		appRepo:    appRepo,
		buildRepo:  buildRepo,
		executor:   executor,
		logQuerier: logQuerier,
	}
}

type CreateBuildRequest struct {
	GitRepo    string `json:"git_repo"`
	GitRef     string `json:"git_ref"`
	ImageTag   string `json:"image_tag"`
	ContextDir string `json:"context_dir"`
}

func (s *BuildService) CreateBuild(ctx context.Context, appName string, req CreateBuildRequest) (*domain.Build, error) {
	if err := domain.ValidateGitRepo(req.GitRepo); err != nil {
		return nil, err
	}
	if err := domain.ValidateGitRef(req.GitRef); err != nil {
		return nil, err
	}

	app, err := s.appRepo.FindByName(ctx, appName)
	if err != nil {
		return nil, err
	}
	if req.GitRef == "" {
		req.GitRef = "main"
	}
	if req.ImageTag == "" {
		req.ImageTag = fmt.Sprintf("%s/%s:%s", app.Image, appName, req.GitRef)
	}

	// ContextDir 验证
	contextDir := req.ContextDir
	if contextDir == "" {
		contextDir = "."
	}
	if err := domain.ValidateContextDir(contextDir); err != nil {
		return nil, err
	}

	now := time.Now()
	build := &domain.Build{
		ID:         uuid.New().String(),
		AppName:    appName,
		GitRepo:    req.GitRepo,
		GitRef:     req.GitRef,
		ImageTag:   req.ImageTag,
		ContextDir: contextDir,
		Status:     domain.BuildStatusPending,
		CreatedAt:  now,
		UpdatedAt:  now,
	}
	if err := s.buildRepo.Save(ctx, build); err != nil {
		return nil, err
	}

	if s.executor != nil {
		jobName, err := s.executor.Submit(ctx, build)
		if err != nil {
			build.Status = domain.BuildStatusFailed
			build.Log = err.Error()
			_ = s.buildRepo.Update(ctx, build)
			return build, nil
		}
		build.JobName = jobName
		build.Status = domain.BuildStatusRunning
		_ = s.buildRepo.Update(ctx, build)
	}

	return build, nil
}

func (s *BuildService) GetBuild(ctx context.Context, appName, id string) (*domain.Build, error) {
	build, err := s.buildRepo.FindByID(ctx, id)
	if err != nil {
		return nil, err
	}
	if build.AppName != appName {
		return nil, domain.ErrBuildNotFound
	}
	return build, nil
}

func (s *BuildService) ListBuilds(ctx context.Context, appName string) ([]*domain.Build, error) {
	if _, err := s.appRepo.FindByName(ctx, appName); err != nil {
		return nil, err
	}
	return s.buildRepo.FindByApp(ctx, appName)
}

func (s *BuildService) CancelBuild(ctx context.Context, appName, id string) error {
	build, err := s.GetBuild(ctx, appName, id)
	if err != nil {
		return err
	}
	if !build.CanCancel() {
		return domain.ErrCannotCancel
	}
	if s.executor != nil && build.JobName != "" {
		if err := s.executor.Cancel(ctx, build.JobName); err != nil {
			return err
		}
	}
	build.Status = domain.BuildStatusCancelled
	build.UpdatedAt = time.Now()
	return s.buildRepo.Update(ctx, build)
}

// GetBuildLogs 获取构建日志。三级降级：Pod logs → Loki → build.Log。
func (s *BuildService) GetBuildLogs(ctx context.Context, appName, id string) (string, error) {
	build, err := s.GetBuild(ctx, appName, id)
	if err != nil {
		return "", err
	}

	// pending 状态还没有 Pod，返回空
	if build.Status == domain.BuildStatusPending {
		return "", nil
	}

	// 1. 尝试从 Pod 读实时日志
	if s.executor != nil {
		logs, err := s.executor.GetLogs(ctx, build.ID)
		if err != nil {
			slog.Warn("failed to get pod logs, trying loki", "build_id", id, "error", err)
		} else if logs != "" {
			return logs, nil
		}
	}

	// 2. 尝试从 Loki 查询历史日志
	if s.logQuerier != nil {
		start := build.CreatedAt.Add(-1 * time.Minute)
		end := build.UpdatedAt.Add(5 * time.Minute)
		logs, err := s.logQuerier.QueryBuildLogs(ctx, "paas-builds", build.ID, start, end)
		if err != nil {
			slog.Warn("failed to get loki logs, falling back to build.Log", "build_id", id, "error", err)
		} else if logs != "" {
			return logs, nil
		}
	}

	// 3. 降级：返回存储的日志
	return build.Log, nil
}

// OnBuildStatusChange 是 Informer callback，更新 Build 状态。
func (s *BuildService) OnBuildStatusChange(buildID string, status domain.BuildStatus, logMsg string) {
	ctx := context.Background()
	build, err := s.buildRepo.FindByID(ctx, buildID)
	if err != nil {
		slog.Error("OnBuildStatusChange: failed to find build", "build_id", buildID, "error", err)
		return
	}
	// 终态不允许被覆盖
	if build.Status.IsTerminal() {
		return
	}
	build.Status = status
	build.Log = logMsg
	build.UpdatedAt = time.Now()
	if err := s.buildRepo.Update(ctx, build); err != nil {
		slog.Error("OnBuildStatusChange: failed to update build", "build_id", buildID, "error", err)
	}
}
