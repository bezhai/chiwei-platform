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
	laneRepo := repository.NewLaneRepo(db)
	buildRepo := repository.NewBuildRepo(db)
	releaseRepo := repository.NewReleaseRepo(db)

	// K8s 客户端（可选，无集群时降级运行）
	cs, _, k8sErr := kubernetes.NewClientset(cfg.KubeconfigPath)
	if k8sErr != nil {
		slog.Warn("k8s client unavailable, running without k8s integration", "error", k8sErr)
	}

	var deployer port.Deployer
	var buildExecutor port.BuildExecutor

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
	}

	// Loki 日志查询
	lokiClient := loki.NewClient(cfg.LokiURL)

	// 服务层
	laneSvc := service.NewLaneService(laneRepo, releaseRepo)
	appSvc := service.NewAppService(appRepo, imageRepoRepo, releaseRepo)
	imageRepoSvc := service.NewImageRepoService(imageRepoRepo, appRepo)
	buildSvc := service.NewBuildService(imageRepoRepo, buildRepo, buildExecutor, lokiClient)
	releaseSvc := service.NewReleaseService(appRepo, imageRepoRepo, laneRepo, releaseRepo, deployer)
	logSvc := service.NewLogService(appRepo, lokiClient, cfg.DeployNamespace)

	// 确保 prod 泳道存在
	ctx := context.Background()
	if err := laneSvc.EnsureDefaultLane(ctx); err != nil {
		slog.Error("failed to ensure default lane", "error", err)
		os.Exit(1)
	}

	// 启动 Build Informer
	if buildExecutor != nil {
		go func() {
			if err := buildExecutor.Watch(ctx, buildSvc.OnBuildStatusChange); err != nil {
				slog.Error("build informer error", "error", err)
			}
		}()
	}

	// HTTP 路由
	handler := httpadapter.NewRouter(
		httpadapter.NewAppHandler(appSvc, buildSvc),
		httpadapter.NewReleaseHandler(releaseSvc),
		httpadapter.NewLaneHandler(laneSvc),
		httpadapter.NewLogHandler(logSvc),
		httpadapter.NewImageRepoHandler(imageRepoSvc),
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
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("server shutdown error", "error", err)
	}
}
