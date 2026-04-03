package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	httpadapter "github.com/chiwei-platform/paas-engine/internal/adapter/http"
	"github.com/chiwei-platform/paas-engine/internal/adapter/kubernetes"
	"github.com/chiwei-platform/paas-engine/internal/adapter/loki"
	"github.com/chiwei-platform/paas-engine/internal/adapter/repository"
	"github.com/chiwei-platform/paas-engine/internal/config"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"github.com/chiwei-platform/paas-engine/internal/service"
	"gorm.io/gorm"
)

func main() {
	cfg := config.Load()

	// 数据库
	db, err := repository.OpenDB(cfg.DatabaseURL)
	if err != nil {
		slog.Error("failed to open db", "error", err)
		os.Exit(1)
	}

	// 存储层
	appRepo := repository.NewAppRepo(db)
	imageRepoRepo := repository.NewImageRepoRepo(db)
	buildRepo := repository.NewBuildRepo(db)
	releaseRepo := repository.NewReleaseRepo(db)
	ciConfigRepo := repository.NewCIConfigRepo(db)
	pipelineRunRepo := repository.NewPipelineRunRepo(db)
	configBundleRepo := repository.NewConfigBundleRepo(db)

	// K8s 客户端（可选，无集群时降级运行）
	cs, _, k8sErr := kubernetes.NewClientset(cfg.KubeconfigPath)
	if k8sErr != nil {
		slog.Warn("k8s client unavailable, running without k8s integration", "error", k8sErr)
	}

	var deployer port.Deployer
	var buildExecutor port.BuildExecutor
	var testExecutor port.TestExecutor

	if cs != nil {
		deployer = kubernetes.NewK8sDeployer(cs, cfg.DeployNamespace)
		buildExecutor = kubernetes.NewKanikoBuildExecutor(cs, kubernetes.KanikoBuildConfig{
			Namespace:          cfg.KanikoNamespace,
			KanikoImage:        cfg.KanikoImage,
			RegistrySecret:     cfg.RegistrySecret,
			RegistryMirrors:    cfg.RegistryMirrors,
			InsecureRegistries: cfg.InsecureRegistries,
			CacheRepo:          cfg.KanikoCacheRepo,
			HttpProxy:          cfg.BuildHttpProxy,
			NoProxy:            cfg.BuildNoProxy,
		})
		testExecutor = kubernetes.NewK8sTestExecutor(cs, kubernetes.TestExecutorConfig{
			Namespace: cfg.CINamespace,
			GitRepo:   cfg.CIGitRepo,
			HttpProxy: cfg.BuildHttpProxy,
			NoProxy:   cfg.BuildNoProxy,
		})
	}

	// Loki 日志查询
	lokiClient := loki.NewClient(cfg.LokiURL)

	// 服务层
	appSvc := service.NewAppService(appRepo, imageRepoRepo, releaseRepo, configBundleRepo)
	imageRepoSvc := service.NewImageRepoService(imageRepoRepo, appRepo)
	buildSvc := service.NewBuildService(imageRepoRepo, buildRepo, buildExecutor, lokiClient)
	configBundleSvc := service.NewConfigBundleService(configBundleRepo, appRepo, releaseRepo)
	releaseSvc := service.NewReleaseService(appRepo, imageRepoRepo, buildRepo, releaseRepo, deployer, configBundleSvc)
	logSvc := service.NewLogService(appRepo, lokiClient, cfg.DeployNamespace)
	pipelineSvc := service.NewPipelineService(ciConfigRepo, pipelineRunRepo, testExecutor, buildSvc, releaseSvc, appRepo, imageRepoRepo, lokiClient, cfg.CINamespace)

	// 启动 Build Informer
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if buildExecutor != nil {
		go func() {
			if err := buildExecutor.Watch(ctx, buildSvc.OnBuildStatusChange); err != nil {
				slog.Error("build informer error", "error", err)
			}
		}()
	}

	// 启动 Test Informer
	if testExecutor != nil {
		go func() {
			if err := testExecutor.Watch(ctx, pipelineSvc.OnTestJobStatusChange); err != nil {
				slog.Error("test informer error", "error", err)
			}
		}()
	}

	// 启动 Git Poller（需要 GITHUB_TOKEN 和 CI_GIT_REPO）
	if cfg.GitHubToken != "" && cfg.CIGitRepo != "" {
		poller := service.NewGitPoller(ciConfigRepo, pipelineSvc, cfg.CIGitRepo,
			cfg.GitHubToken, cfg.GitPollInterval, cfg.BuildHttpProxy)
		if poller != nil {
			go poller.Start(ctx)
		}
	}

	// Ops 数据库连接池（只读查询）
	opsDbs := map[string]*gorm.DB{"paas_engine": db}
	if cfg.ChiweiDatabaseURL != "" {
		chiweiDB, err := repository.OpenReadOnlyDB(cfg.ChiweiDatabaseURL)
		if err != nil {
			slog.Warn("chiwei database unavailable for ops queries", "error", err)
		} else {
			opsDbs["chiwei"] = chiweiDB
		}
	}

	// Ops 写连接池（用于执行审批通过的 DDL/DML）
	writeDbs := map[string]*gorm.DB{"paas_engine": db}
	if cfg.ChiweiDatabaseURL != "" {
		chiweiWriteDB, err := repository.OpenWriteDB(cfg.ChiweiDatabaseURL)
		if err != nil {
			slog.Warn("chiwei write database unavailable for mutations", "error", err)
		} else {
			writeDbs["chiwei"] = chiweiWriteDB
		}
	}

	mutationRepo := repository.NewMutationRepo(db)

	// HTTP 路由
	handler := httpadapter.NewRouter(
		httpadapter.NewAppHandler(appSvc, buildSvc),
		httpadapter.NewReleaseHandler(releaseSvc),
		httpadapter.NewLogHandler(logSvc),
		httpadapter.NewImageRepoHandler(imageRepoSvc),
		httpadapter.NewOpsHandler(opsDbs, writeDbs, mutationRepo),
		httpadapter.NewPipelineHandler(pipelineSvc),
		httpadapter.NewConfigBundleHandler(configBundleSvc),
		cfg.APIToken,
	)

	srv := &http.Server{
		Addr:    ":" + cfg.HTTPPort,
		Handler: handler,
	}

	go func() {
		slog.Info("server starting", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("server error", "error", err)
			os.Exit(1)
		}
	}()

	// Graceful shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	slog.Info("shutting down server")
	cancel() // 停止 git poller + informer
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer shutdownCancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("server shutdown error", "error", err)
	}
}
