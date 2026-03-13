package service

import (
	"context"
	"fmt"
	"strings"
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

// LogQueryOptions 通用日志查询参数。
type LogQueryOptions struct {
	App       string // 逗号分隔多 app，空=查全部
	Lane      string
	Pod       string
	Since     string // 与 Start/End 二选一
	Start     string // RFC3339，优先于 Since
	End       string // RFC3339，不填=now
	Limit     int
	Keyword   string
	Exclude   string
	Regexp    string
	Direction string
}

// QueryLogs 通用日志查询，支持多 app / 关键字过滤 / direction。
func (s *LogService) QueryLogs(ctx context.Context, opts LogQueryOptions) (string, error) {
	// 解析 apps
	var apps []string
	if opts.App != "" {
		for _, a := range strings.Split(opts.App, ",") {
			a = strings.TrimSpace(a)
			if a == "" {
				continue
			}
			// 验证 app 存在
			if _, err := s.appRepo.FindByName(ctx, a); err != nil {
				return "", err
			}
			apps = append(apps, a)
		}
	}

	// 解析时间范围：start/end 优先，否则用 since
	var start, end time.Time
	if opts.Start != "" {
		var err error
		start, err = time.Parse(time.RFC3339, opts.Start)
		if err != nil {
			return "", fmt.Errorf("%w: invalid start %q: %v", domain.ErrInvalidInput, opts.Start, err)
		}
		if opts.End != "" {
			end, err = time.Parse(time.RFC3339, opts.End)
			if err != nil {
				return "", fmt.Errorf("%w: invalid end %q: %v", domain.ErrInvalidInput, opts.End, err)
			}
		} else {
			end = time.Now()
		}
	} else {
		since := opts.Since
		if since == "" {
			since = "1h"
		}
		duration, err := time.ParseDuration(since)
		if err != nil {
			return "", fmt.Errorf("%w: invalid since %q: %v", domain.ErrInvalidInput, since, err)
		}
		if duration <= 0 {
			return "", fmt.Errorf("%w: since must be positive", domain.ErrInvalidInput)
		}
		end = time.Now()
		start = end.Add(-duration)
	}

	// 校验 limit
	limit := opts.Limit
	if limit <= 0 {
		limit = 1000
	}
	if limit > 5000 {
		limit = 5000
	}

	// 校验 direction
	direction := opts.Direction
	if direction != "forward" && direction != "backward" {
		direction = "backward"
	}

	query := port.AppLogQuery{
		Namespace: s.deployNamespace,
		Apps:      apps,
		Lane:      opts.Lane,
		Pod:       opts.Pod,
		Keyword:   opts.Keyword,
		Exclude:   opts.Exclude,
		Regexp:    opts.Regexp,
		Start:     start,
		End:       end,
		Limit:     limit,
		Direction: direction,
	}

	return s.logQuerier.QueryAppLogs(ctx, query)
}

// GetAppLogs 向后兼容：查询单 app 运行时日志。
func (s *LogService) GetAppLogs(ctx context.Context, appName, lane, since string, limit int) (string, error) {
	return s.QueryLogs(ctx, LogQueryOptions{
		App:       appName,
		Lane:      lane,
		Since:     since,
		Limit:     limit,
		Direction: "forward",
	})
}
