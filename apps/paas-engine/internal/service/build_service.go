package service

import (
	"context"
	"log/slog"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"github.com/google/uuid"
)

type BuildService struct {
	imageRepoRepo port.ImageRepoRepository
	buildRepo     port.BuildRepository
	executor      port.BuildExecutor
	logQuerier    port.LogQuerier
}

func NewBuildService(
	imageRepoRepo port.ImageRepoRepository,
	buildRepo port.BuildRepository,
	executor port.BuildExecutor,
	logQuerier port.LogQuerier,
) *BuildService {
	return &BuildService{
		imageRepoRepo: imageRepoRepo,
		buildRepo:     buildRepo,
		executor:      executor,
		logQuerier:    logQuerier,
	}
}

type CreateBuildRequest struct {
	GitRef   string `json:"git_ref"`
	ImageTag string `json:"image_tag"` // tag 部分（如 abc123），service 层拼完整 URL
}

func (s *BuildService) CreateBuild(ctx context.Context, imageRepoName string, req CreateBuildRequest) (*domain.Build, error) {
	imageRepo, err := s.imageRepoRepo.FindByName(ctx, imageRepoName)
	if err != nil {
		return nil, err
	}

	if req.GitRef == "" {
		req.GitRef = "main"
	}
	if err := domain.ValidateGitRef(req.GitRef); err != nil {
		return nil, err
	}

	// image tag: 请求值 → git ref
	tag := req.ImageTag
	if tag == "" {
		tag = req.GitRef
	}
	fullImageRef := imageRepo.FullImageRef(tag)

	now := time.Now()
	build := &domain.Build{
		ID:            uuid.New().String(),
		ImageRepoName: imageRepoName,
		GitRef:        req.GitRef,
		ImageTag:      fullImageRef,
		Status:        domain.BuildStatusPending,
		CreatedAt:     now,
		UpdatedAt:     now,
	}
	if err := s.buildRepo.Save(ctx, build); err != nil {
		return nil, err
	}

	if s.executor != nil {
		sub := &port.BuildSubmission{
			BuildID:    build.ID,
			GitRepo:    imageRepo.GitRepo,
			GitRef:     req.GitRef,
			ContextDir: imageRepo.ContextDir,
			Dockerfile: imageRepo.Dockerfile,
			ImageTag:   fullImageRef,
		}
		jobName, err := s.executor.Submit(ctx, sub)
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

func (s *BuildService) GetBuild(ctx context.Context, imageRepoName, id string) (*domain.Build, error) {
	build, err := s.buildRepo.FindByID(ctx, id)
	if err != nil {
		return nil, err
	}
	if build.ImageRepoName != imageRepoName {
		return nil, domain.ErrBuildNotFound
	}
	return build, nil
}

func (s *BuildService) GetLatestSuccessfulBuild(ctx context.Context, imageRepoName string) (*domain.Build, error) {
	if _, err := s.imageRepoRepo.FindByName(ctx, imageRepoName); err != nil {
		return nil, err
	}
	return s.buildRepo.FindLatestSuccessful(ctx, imageRepoName)
}

func (s *BuildService) ListBuilds(ctx context.Context, imageRepoName string) ([]*domain.Build, error) {
	if _, err := s.imageRepoRepo.FindByName(ctx, imageRepoName); err != nil {
		return nil, err
	}
	return s.buildRepo.FindByImageRepo(ctx, imageRepoName)
}

func (s *BuildService) CancelBuild(ctx context.Context, imageRepoName, id string) error {
	build, err := s.GetBuild(ctx, imageRepoName, id)
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
func (s *BuildService) GetBuildLogs(ctx context.Context, imageRepoName, id string) (string, error) {
	build, err := s.GetBuild(ctx, imageRepoName, id)
	if err != nil {
		return "", err
	}

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
