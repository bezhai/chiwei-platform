package service

import (
	"context"
	"fmt"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type LogService struct {
	appRepo         port.AppRepository
	logQuerier      port.LogQuerier
	deployNamespace string
}

func NewLogService(appRepo port.AppRepository, logQuerier port.LogQuerier, deployNamespace string) *LogService {
	return &LogService{
		appRepo:         appRepo,
		logQuerier:      logQuerier,
		deployNamespace: deployNamespace,
	}
}

// GetAppLogs 查询运行时日志。since 为 Go duration 字符串（如 "1h"），limit 上限 5000。
func (s *LogService) GetAppLogs(ctx context.Context, appName, lane, since string, limit int) (string, error) {
	if _, err := s.appRepo.FindByName(ctx, appName); err != nil {
		return "", err
	}

	duration, err := time.ParseDuration(since)
	if err != nil {
		return "", fmt.Errorf("%w: invalid since %q: %v", domain.ErrInvalidInput, since, err)
	}
	if duration <= 0 {
		return "", fmt.Errorf("%w: since must be positive", domain.ErrInvalidInput)
	}

	if limit <= 0 {
		limit = 1000
	}
	if limit > 5000 {
		limit = 5000
	}

	end := time.Now()
	start := end.Add(-duration)

	return s.logQuerier.QueryAppLogs(ctx, s.deployNamespace, appName, lane, start, end, limit)
}
