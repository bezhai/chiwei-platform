package service

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"github.com/google/uuid"
)

type PipelineService struct {
	ciConfigRepo port.CIConfigRepository
	pipelineRepo port.PipelineRunRepository
	testExecutor port.TestExecutor
	buildSvc     *BuildService
	releaseSvc   *ReleaseService
	appRepo      port.AppRepository
	imageRepo    port.ImageRepoRepository
	logQuerier   port.LogQuerier
	ciNamespace  string
}

func NewPipelineService(
	ciConfigRepo port.CIConfigRepository,
	pipelineRepo port.PipelineRunRepository,
	testExecutor port.TestExecutor,
	buildSvc *BuildService,
	releaseSvc *ReleaseService,
	appRepo port.AppRepository,
	imageRepo port.ImageRepoRepository,
	logQuerier port.LogQuerier,
	ciNamespace string,
) *PipelineService {
	return &PipelineService{
		ciConfigRepo: ciConfigRepo,
		pipelineRepo: pipelineRepo,
		testExecutor: testExecutor,
		buildSvc:     buildSvc,
		releaseSvc:   releaseSvc,
		appRepo:      appRepo,
		imageRepo:    imageRepo,
		logQuerier:   logQuerier,
		ciNamespace:  ciNamespace,
	}
}

// RegisterCIRequest 注册 CI 泳道的请求。
type RegisterCIRequest struct {
	Lane     string   `json:"lane"`
	Branch   string   `json:"branch"`
	Services []string `json:"services"`
}

// RegisterCI 注册一个 CI 泳道配置。
func (s *PipelineService) RegisterCI(ctx context.Context, req RegisterCIRequest) (*domain.CIConfig, error) {
	if req.Lane == "" || req.Branch == "" || len(req.Services) == 0 {
		return nil, fmt.Errorf("%w: lane, branch, and services are required", domain.ErrInvalidInput)
	}
	if err := domain.ValidateK8sName(req.Lane); err != nil {
		return nil, err
	}
	if req.Lane == domain.DefaultLane {
		return nil, fmt.Errorf("%w: cannot register CI for prod lane", domain.ErrInvalidInput)
	}

	now := time.Now()
	cfg := &domain.CIConfig{
		ID:        uuid.New().String(),
		Lane:      req.Lane,
		Branch:    req.Branch,
		Services:  req.Services,
		Status:    "active",
		CreatedAt: now,
		UpdatedAt: now,
	}
	if err := s.ciConfigRepo.Save(ctx, cfg); err != nil {
		return nil, err
	}
	slog.Info("ci config registered", "lane", req.Lane, "branch", req.Branch, "services", req.Services)
	return cfg, nil
}

// UnregisterCI 注销 CI 泳道，删除泳道 Release 并归档配置。
func (s *PipelineService) UnregisterCI(ctx context.Context, lane string) error {
	cfg, err := s.ciConfigRepo.FindByLane(ctx, lane)
	if err != nil {
		return err
	}

	// 删除泳道上所有 Release
	for _, svcName := range cfg.Services {
		if err := s.releaseSvc.DeleteReleaseByAppAndLane(ctx, svcName, lane); err != nil {
			slog.Warn("failed to delete release during ci cleanup", "app", svcName, "lane", lane, "error", err)
		}
	}

	cfg.Status = "archived"
	cfg.UpdatedAt = time.Now()
	return s.ciConfigRepo.Update(ctx, cfg)
}

// ListCIConfigs 列出所有活跃的 CI 配置。
func (s *PipelineService) ListCIConfigs(ctx context.Context) ([]*domain.CIConfig, error) {
	return s.ciConfigRepo.FindActive(ctx)
}

// GetCIConfig 获取指定泳道的 CI 配置。
func (s *PipelineService) GetCIConfig(ctx context.Context, lane string) (*domain.CIConfig, error) {
	return s.ciConfigRepo.FindByLane(ctx, lane)
}

// TriggerPipelineRequest 手动触发 pipeline 的请求。
type TriggerPipelineRequest struct {
	CommitSHA string `json:"commit_sha,omitempty"`
}

// TriggerPipeline 手动触发指定泳道的 pipeline。
func (s *PipelineService) TriggerPipeline(ctx context.Context, lane string, req TriggerPipelineRequest) (*domain.PipelineRun, error) {
	cfg, err := s.ciConfigRepo.FindByLane(ctx, lane)
	if err != nil {
		return nil, err
	}

	commitSHA := req.CommitSHA
	if commitSHA == "" {
		commitSHA = "manual"
	}

	// 幂等检查
	if commitSHA != "manual" {
		exists, err := s.pipelineRepo.ExistsByCommitSHA(ctx, commitSHA)
		if err != nil {
			return nil, err
		}
		if exists {
			return nil, fmt.Errorf("%w: pipeline already exists for commit %s", domain.ErrAlreadyExists, commitSHA)
		}
	}

	now := time.Now()
	run := &domain.PipelineRun{
		ID:         uuid.New().String(),
		CIConfigID: cfg.ID,
		GitRef:     cfg.Branch,
		CommitSHA:  commitSHA,
		Lane:       cfg.Lane,
		Services:   cfg.Services,
		Status:     domain.PipelineRunPending,
		CreatedAt:  now,
		UpdatedAt:  now,
	}
	if err := s.pipelineRepo.Save(ctx, run); err != nil {
		return nil, err
	}

	// 异步执行 pipeline
	go s.runPipeline(context.Background(), run, cfg)

	return run, nil
}

// GetPipelineRun 获取 pipeline run 详情（含嵌套 stages + jobs）。
func (s *PipelineService) GetPipelineRun(ctx context.Context, id string) (*domain.PipelineRun, error) {
	run, err := s.pipelineRepo.FindByID(ctx, id)
	if err != nil {
		return nil, err
	}
	return s.fillRunDetails(ctx, run)
}

// ListPipelineRuns 列出泳道的 pipeline 执行记录。
func (s *PipelineService) ListPipelineRuns(ctx context.Context, lane string, limit int) ([]*domain.PipelineRun, error) {
	if limit <= 0 {
		limit = 10
	}
	return s.pipelineRepo.FindByLane(ctx, lane, limit)
}

// CancelPipelineRun 取消正在执行的 pipeline。
func (s *PipelineService) CancelPipelineRun(ctx context.Context, id string) error {
	run, err := s.pipelineRepo.FindByID(ctx, id)
	if err != nil {
		return err
	}
	if run.Status.IsTerminal() {
		return fmt.Errorf("%w: pipeline run is already %s", domain.ErrCannotCancel, run.Status)
	}

	// 取消所有活跃 Job
	stages, _ := s.pipelineRepo.FindStagesByRunID(ctx, id)
	for _, stage := range stages {
		jobs, _ := s.pipelineRepo.FindJobsByStageID(ctx, stage.ID)
		for _, job := range jobs {
			if !job.Status.IsTerminal() && job.K8sJobName != "" && s.testExecutor != nil {
				_ = s.testExecutor.Cancel(ctx, job.K8sJobName)
			}
			if !job.Status.IsTerminal() {
				job.Status = domain.PipelineRunCancelled
				job.UpdatedAt = time.Now()
				_ = s.pipelineRepo.UpdateJob(ctx, &job)
			}
		}
		if !stage.Status.IsTerminal() {
			stage.Status = domain.PipelineRunCancelled
			stage.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateStage(ctx, &stage)
		}
	}

	run.Status = domain.PipelineRunCancelled
	run.UpdatedAt = time.Now()
	return s.pipelineRepo.Update(ctx, run)
}

// GetJobLogs 获取指定 job 的日志。三级降级：Pod → Loki → DB。
func (s *PipelineService) GetJobLogs(ctx context.Context, jobRunID string) (string, error) {
	job, err := s.pipelineRepo.FindJobByID(ctx, jobRunID)
	if err != nil {
		return "", err
	}

	// 1. 尝试从 K8s Pod 读实时日志
	if s.testExecutor != nil && job.JobType == string(domain.StageUnitTest) {
		logs, err := s.testExecutor.GetLogs(ctx, job.ID)
		if err == nil && logs != "" {
			return logs, nil
		}
		if err != nil {
			slog.Warn("failed to get pod logs for ci job, trying loki", "job_id", jobRunID, "error", err)
		}
	}

	// 2. 尝试从 Loki 查询历史日志
	if s.logQuerier != nil && s.ciNamespace != "" && job.JobType == string(domain.StageUnitTest) {
		podPrefix := "ci-test-" + strings.ReplaceAll(job.ID, "-", "")[:24]
		start := job.CreatedAt.Add(-1 * time.Minute)
		end := job.UpdatedAt.Add(5 * time.Minute)
		query := port.AppLogQuery{
			Namespace: s.ciNamespace,
			Pod:       podPrefix,
			Start:     start,
			End:       end,
			Limit:     5000,
			Direction: "forward",
		}
		logs, err := s.logQuerier.QueryAppLogs(ctx, query)
		if err != nil {
			slog.Warn("failed to get loki logs for ci job, falling back to db", "job_id", jobRunID, "error", err)
		} else if logs != "" {
			return logs, nil
		}
	}

	// 3. 降级：返回 DB 中存储的日志
	return job.Log, nil
}

// OnTestJobStatusChange 是 TestExecutor Informer 的 callback。
func (s *PipelineService) OnTestJobStatusChange(jobRunID string, status domain.PipelineRunStatus, logMsg string) {
	ctx := context.Background()
	job, err := s.pipelineRepo.FindJobByID(ctx, jobRunID)
	if err != nil {
		slog.Error("OnTestJobStatusChange: failed to find job", "job_run_id", jobRunID, "error", err)
		return
	}
	if job.Status.IsTerminal() {
		return
	}
	job.Status = status
	if logMsg != "" {
		job.Log = logMsg
	}
	job.UpdatedAt = time.Now()
	if err := s.pipelineRepo.UpdateJob(ctx, job); err != nil {
		slog.Error("OnTestJobStatusChange: failed to update job", "job_run_id", jobRunID, "error", err)
	}
}

// runPipeline 执行特性分支 pipeline：unit-test → build → deploy。
func (s *PipelineService) runPipeline(ctx context.Context, run *domain.PipelineRun, _ *domain.CIConfig) {
	slog.Info("pipeline started", "id", run.ID, "lane", run.Lane, "services", run.Services)

	run.Status = domain.PipelineRunRunning
	run.UpdatedAt = time.Now()
	_ = s.pipelineRepo.Update(ctx, run)

	stages := []struct {
		stage   domain.StageType
		handler func(ctx context.Context, run *domain.PipelineRun, stage *domain.StageRun) error
	}{
		{domain.StageUnitTest, s.runUnitTestStage},
		{domain.StageBuild, s.runBuildStage},
		{domain.StageDeploy, s.runDeployStage},
	}

	for seq, st := range stages {
		now := time.Now()
		stage := &domain.StageRun{
			ID:            uuid.New().String(),
			PipelineRunID: run.ID,
			Stage:         st.stage,
			Seq:           seq + 1,
			Status:        domain.PipelineRunRunning,
			CreatedAt:     now,
			UpdatedAt:     now,
		}
		_ = s.pipelineRepo.SaveStage(ctx, stage)

		if err := st.handler(ctx, run, stage); err != nil {
			stage.Status = domain.PipelineRunFailed
			stage.Message = err.Error()
			stage.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateStage(ctx, stage)

			run.Status = domain.PipelineRunFailed
			run.Message = fmt.Sprintf("stage %s failed: %s", st.stage, err.Error())
			run.UpdatedAt = time.Now()
			_ = s.pipelineRepo.Update(ctx, run)
			slog.Error("pipeline failed", "id", run.ID, "stage", st.stage, "error", err)
			return
		}

		stage.Status = domain.PipelineRunSucceeded
		stage.UpdatedAt = time.Now()
		_ = s.pipelineRepo.UpdateStage(ctx, stage)
	}

	run.Status = domain.PipelineRunSucceeded
	run.UpdatedAt = time.Now()
	_ = s.pipelineRepo.Update(ctx, run)
	slog.Info("pipeline succeeded", "id", run.ID, "lane", run.Lane)
}

// runUnitTestStage 并行跑注册服务的单测。
func (s *PipelineService) runUnitTestStage(ctx context.Context, run *domain.PipelineRun, stage *domain.StageRun) error {
	if s.testExecutor == nil {
		slog.Warn("test executor not configured, skipping unit tests")
		return nil
	}

	var wg sync.WaitGroup
	errs := make(chan error, len(run.Services))

	for _, svcName := range run.Services {
		wg.Add(1)
		go func(svc string) {
			defer wg.Done()

			now := time.Now()
			job := &domain.JobRun{
				ID:         uuid.New().String(),
				StageRunID: stage.ID,
				Name:       svc,
				JobType:    string(domain.StageUnitTest),
				Status:     domain.PipelineRunPending,
				CreatedAt:  now,
				UpdatedAt:  now,
			}
			_ = s.pipelineRepo.SaveJob(ctx, job)

			// 确定 runtime 和命令（使用约定的默认值）
			runtime, cmd := s.resolveUnitTestCommand(svc)
			if cmd == "" {
				job.Status = domain.PipelineRunSucceeded
				job.Log = "no unit test configured, skipped"
				job.UpdatedAt = time.Now()
				_ = s.pipelineRepo.UpdateJob(ctx, job)
				return
			}

			sub := &port.TestSubmission{
				JobRunID: job.ID,
				GitRepo:  s.resolveGitRepo(ctx),
				GitRef:   run.GitRef,
				Runtime:  runtime,
				Command:  cmd,
			}

			jobName, err := s.testExecutor.Submit(ctx, sub)
			if err != nil {
				job.Status = domain.PipelineRunFailed
				job.Log = err.Error()
				job.UpdatedAt = time.Now()
				_ = s.pipelineRepo.UpdateJob(ctx, job)
				errs <- fmt.Errorf("service %s unit test submit failed: %w", svc, err)
				return
			}

			job.K8sJobName = jobName
			job.Status = domain.PipelineRunRunning
			job.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateJob(ctx, job)

			// 等待 Job 完成（轮询 DB 状态，由 Informer callback 更新）
			if err := s.waitForJobCompletion(ctx, job.ID, 10*time.Minute); err != nil {
				errs <- fmt.Errorf("service %s unit test: %w", svc, err)
				return
			}
		}(svcName)
	}

	wg.Wait()
	close(errs)

	for err := range errs {
		if err != nil {
			return err
		}
	}
	return nil
}

// runBuildStage 并行构建注册服务的镜像。
func (s *PipelineService) runBuildStage(ctx context.Context, run *domain.PipelineRun, stage *domain.StageRun) error {
	var wg sync.WaitGroup
	errs := make(chan error, len(run.Services))

	for _, svcName := range run.Services {
		wg.Add(1)
		go func(svc string) {
			defer wg.Done()

			now := time.Now()
			job := &domain.JobRun{
				ID:         uuid.New().String(),
				StageRunID: stage.ID,
				Name:       svc,
				JobType:    string(domain.StageBuild),
				Status:     domain.PipelineRunPending,
				CreatedAt:  now,
				UpdatedAt:  now,
			}
			_ = s.pipelineRepo.SaveJob(ctx, job)

			// 查找 App 关联的 ImageRepo
			app, err := s.appRepo.FindByName(ctx, svc)
			if err != nil {
				job.Status = domain.PipelineRunFailed
				job.Log = fmt.Sprintf("app %s not found: %v", svc, err)
				job.UpdatedAt = time.Now()
				_ = s.pipelineRepo.UpdateJob(ctx, job)
				errs <- fmt.Errorf("service %s build: app not found", svc)
				return
			}

			// 使用 BuildService 创建构建
			build, err := s.buildSvc.CreateBuild(ctx, app.ImageRepoName, CreateBuildRequest{
				GitRef: run.GitRef,
			})
			if err != nil {
				job.Status = domain.PipelineRunFailed
				job.Log = err.Error()
				job.UpdatedAt = time.Now()
				_ = s.pipelineRepo.UpdateJob(ctx, job)
				errs <- fmt.Errorf("service %s build failed: %w", svc, err)
				return
			}

			job.RefID = build.ID
			job.Status = domain.PipelineRunRunning
			job.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateJob(ctx, job)

			// 等待 Build 完成
			if err := s.waitForBuildCompletion(ctx, build.ID, 15*time.Minute); err != nil {
				job.Status = domain.PipelineRunFailed
				job.Log = err.Error()
				job.UpdatedAt = time.Now()
				_ = s.pipelineRepo.UpdateJob(ctx, job)
				errs <- fmt.Errorf("service %s build: %w", svc, err)
				return
			}

			job.Status = domain.PipelineRunSucceeded
			job.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateJob(ctx, job)
		}(svcName)
	}

	wg.Wait()
	close(errs)

	for err := range errs {
		if err != nil {
			return err
		}
	}
	return nil
}

// runDeployStage 部署服务到注册泳道。
func (s *PipelineService) runDeployStage(ctx context.Context, run *domain.PipelineRun, stage *domain.StageRun) error {
	for _, svcName := range run.Services {
		now := time.Now()
		job := &domain.JobRun{
			ID:         uuid.New().String(),
			StageRunID: stage.ID,
			Name:       svcName,
			JobType:    string(domain.StageDeploy),
			Status:     domain.PipelineRunRunning,
			CreatedAt:  now,
			UpdatedAt:  now,
		}
		_ = s.pipelineRepo.SaveJob(ctx, job)

		app, err := s.appRepo.FindByName(ctx, svcName)
		if err != nil {
			job.Status = domain.PipelineRunFailed
			job.Log = err.Error()
			job.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateJob(ctx, job)
			return fmt.Errorf("deploy %s: %w", svcName, err)
		}

		// 查找刚构建的最新成功版本
		latestBuild, err := s.buildSvc.GetLatestSuccessfulBuild(ctx, app.ImageRepoName)
		if err != nil {
			job.Status = domain.PipelineRunFailed
			job.Log = err.Error()
			job.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateJob(ctx, job)
			return fmt.Errorf("deploy %s: no successful build found", svcName)
		}

		release, err := s.releaseSvc.CreateOrUpdateRelease(ctx, CreateReleaseRequest{
			AppName:  svcName,
			Lane:     run.Lane,
			ImageTag: latestBuild.Version,
		})
		if err != nil {
			job.Status = domain.PipelineRunFailed
			job.Log = err.Error()
			job.UpdatedAt = time.Now()
			_ = s.pipelineRepo.UpdateJob(ctx, job)
			return fmt.Errorf("deploy %s: %w", svcName, err)
		}

		job.RefID = release.ID
		job.Status = domain.PipelineRunSucceeded
		job.UpdatedAt = time.Now()
		_ = s.pipelineRepo.UpdateJob(ctx, job)
	}
	return nil
}

// fillRunDetails 填充 PipelineRun 的 stages 和 jobs。
func (s *PipelineService) fillRunDetails(ctx context.Context, run *domain.PipelineRun) (*domain.PipelineRun, error) {
	stages, err := s.pipelineRepo.FindStagesByRunID(ctx, run.ID)
	if err != nil {
		return run, nil
	}
	for i := range stages {
		jobs, err := s.pipelineRepo.FindJobsByStageID(ctx, stages[i].ID)
		if err == nil {
			stages[i].Jobs = jobs
		}
	}
	run.Stages = stages
	return run, nil
}

// resolveGitRepo 获取 monorepo 的 git 地址。
func (s *PipelineService) resolveGitRepo(ctx context.Context) string {
	repos, err := s.imageRepo.FindAll(ctx)
	if err != nil || len(repos) == 0 {
		return ""
	}
	return repos[0].GitRepo
}

// resolveUnitTestCommand 根据服务名推断 runtime 和测试命令。
func (s *PipelineService) resolveUnitTestCommand(svcName string) (runtime, cmd string) {
	// 约定：按 app 已知信息推断
	// 未来由 pipeline.yml 提供，Phase 0 使用硬编码默认值
	knownServices := map[string][2]string{
		"paas-engine":   {"go", "cd apps/paas-engine && go test ./... -v -count=1"},
		"agent-service": {"python", "cd apps/agent-service && uv run pytest tests/ -v"},
		"lark-server":   {"bun", "cd apps/lark-server && bun test"},
		"lark-proxy":    {"bun", "cd apps/lark-proxy && bun test"},
		"tool-service":  {"python", "cd apps/tool-service && uv run pytest tests/ -v"},
	}
	if info, ok := knownServices[svcName]; ok {
		return info[0], info[1]
	}
	return "", ""
}

// waitForJobCompletion 轮询 DB 等待 JobRun 完成。
func (s *PipelineService) waitForJobCompletion(ctx context.Context, jobID string, timeout time.Duration) error {
	deadline := time.After(timeout)
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline:
			return fmt.Errorf("timeout waiting for job %s", jobID)
		case <-ticker.C:
			job, err := s.pipelineRepo.FindJobByID(ctx, jobID)
			if err != nil {
				return err
			}
			switch job.Status {
			case domain.PipelineRunSucceeded:
				return nil
			case domain.PipelineRunFailed:
				return fmt.Errorf("job failed: %s", job.Log)
			case domain.PipelineRunCancelled:
				return fmt.Errorf("job cancelled")
			}
		}
	}
}

// waitForBuildCompletion 轮询 DB 等待 Build 完成。
func (s *PipelineService) waitForBuildCompletion(ctx context.Context, buildID string, timeout time.Duration) error {
	deadline := time.After(timeout)
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline:
			return fmt.Errorf("timeout waiting for build %s", buildID)
		case <-ticker.C:
			build, err := s.buildSvc.buildRepo.FindByID(ctx, buildID)
			if err != nil {
				return err
			}
			switch build.Status {
			case domain.BuildStatusSucceeded:
				return nil
			case domain.BuildStatusFailed:
				return fmt.Errorf("build failed: %s", build.Log)
			case domain.BuildStatusCancelled:
				return fmt.Errorf("build cancelled")
			}
		}
	}
}
