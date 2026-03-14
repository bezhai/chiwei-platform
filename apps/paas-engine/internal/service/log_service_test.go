package service

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// --- stubs ---

type stubAppRepo struct {
	app *domain.App
	err error
}

func (s *stubAppRepo) Save(_ context.Context, _ *domain.App) error        { return nil }
func (s *stubAppRepo) Update(_ context.Context, _ *domain.App) error      { return nil }
func (s *stubAppRepo) Delete(_ context.Context, _ string) error           { return nil }
func (s *stubAppRepo) FindAll(_ context.Context) ([]*domain.App, error)   { return nil, nil }
func (s *stubAppRepo) FindByName(_ context.Context, _ string) (*domain.App, error) {
	return s.app, s.err
}

type stubLogQuerier struct {
	logs string
	err  error
	// 捕获最后一次调用参数
	lastQuery port.AppLogQuery
}

func (s *stubLogQuerier) QueryBuildLogs(_ context.Context, _, _ string, _, _ time.Time) (string, error) {
	return "", nil
}

func (s *stubLogQuerier) QueryAppLogs(_ context.Context, query port.AppLogQuery) (string, error) {
	s.lastQuery = query
	return s.logs, s.err
}

// --- tests ---

func TestLogService_GetAppLogs_Success(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	querier := &stubLogQuerier{logs: "line1\nline2\n"}
	svc := NewLogService(appRepo, querier, "prod")

	logs, err := svc.GetAppLogs(context.Background(), "myapp", "prod", "1h", 500)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if logs != "line1\nline2\n" {
		t.Errorf("got %q", logs)
	}
	if querier.lastQuery.Namespace != "prod" {
		t.Errorf("namespace = %q, want prod", querier.lastQuery.Namespace)
	}
	if querier.lastQuery.Lane != "prod" {
		t.Errorf("lane = %q, want prod", querier.lastQuery.Lane)
	}
	if querier.lastQuery.Limit != 500 {
		t.Errorf("limit = %d, want 500", querier.lastQuery.Limit)
	}
	if len(querier.lastQuery.Apps) != 1 || querier.lastQuery.Apps[0] != "myapp" {
		t.Errorf("apps = %v, want [myapp]", querier.lastQuery.Apps)
	}
}

func TestLogService_GetAppLogs_NoLane(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	querier := &stubLogQuerier{logs: "all lanes\n"}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.GetAppLogs(context.Background(), "myapp", "", "30m", 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if querier.lastQuery.Lane != "" {
		t.Errorf("expected empty lane, got %q", querier.lastQuery.Lane)
	}
	if querier.lastQuery.Limit != 1000 {
		t.Errorf("limit = %d, want 1000", querier.lastQuery.Limit)
	}
}

func TestLogService_GetAppLogs_LimitCapped(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.GetAppLogs(context.Background(), "myapp", "", "1h", 9999)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if querier.lastQuery.Limit != 5000 {
		t.Errorf("limit should be capped at 5000, got %d", querier.lastQuery.Limit)
	}
}

func TestLogService_GetAppLogs_AppNotFound(t *testing.T) {
	appRepo := &stubAppRepo{err: domain.ErrAppNotFound}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.GetAppLogs(context.Background(), "nonexistent", "", "1h", 100)
	if !errors.Is(err, domain.ErrNotFound) {
		t.Errorf("expected ErrNotFound, got %v", err)
	}
}

func TestLogService_GetAppLogs_InvalidSince(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.GetAppLogs(context.Background(), "myapp", "", "bad-duration", 100)
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestLogService_GetAppLogs_NegativeSince(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.GetAppLogs(context.Background(), "myapp", "", "-1h", 100)
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Errorf("expected ErrInvalidInput, got %v", err)
	}
}

func TestQueryLogs_NoApp(t *testing.T) {
	appRepo := &stubAppRepo{}
	querier := &stubLogQuerier{logs: "all apps\n"}
	svc := NewLogService(appRepo, querier, "prod")

	logs, err := svc.QueryLogs(context.Background(), LogQueryOptions{
		Since: "30m",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if logs != "all apps\n" {
		t.Errorf("got %q", logs)
	}
	if len(querier.lastQuery.Apps) != 0 {
		t.Errorf("expected no apps, got %v", querier.lastQuery.Apps)
	}
}

func TestQueryLogs_WithKeywordAndExclude(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.QueryLogs(context.Background(), LogQueryOptions{
		App:     "myapp",
		Keyword: "error",
		Exclude: "health",
		Regexp:  "timeout|deadline",
		Since:   "1h",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if querier.lastQuery.Keyword != "error" {
		t.Errorf("keyword = %q, want error", querier.lastQuery.Keyword)
	}
	if querier.lastQuery.Exclude != "health" {
		t.Errorf("exclude = %q, want health", querier.lastQuery.Exclude)
	}
	if querier.lastQuery.Regexp != "timeout|deadline" {
		t.Errorf("regexp = %q, want timeout|deadline", querier.lastQuery.Regexp)
	}
}

func TestQueryLogs_DirectionValidation(t *testing.T) {
	appRepo := &stubAppRepo{}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	// valid direction
	_, _ = svc.QueryLogs(context.Background(), LogQueryOptions{Direction: "forward", Since: "1h"})
	if querier.lastQuery.Direction != "forward" {
		t.Errorf("direction = %q, want forward", querier.lastQuery.Direction)
	}

	_, _ = svc.QueryLogs(context.Background(), LogQueryOptions{Direction: "backward", Since: "1h"})
	if querier.lastQuery.Direction != "backward" {
		t.Errorf("direction = %q, want backward", querier.lastQuery.Direction)
	}

	// invalid direction defaults to backward
	_, _ = svc.QueryLogs(context.Background(), LogQueryOptions{Direction: "invalid", Since: "1h"})
	if querier.lastQuery.Direction != "backward" {
		t.Errorf("direction = %q, want backward (default)", querier.lastQuery.Direction)
	}
}

func TestQueryLogs_StartEndPriority(t *testing.T) {
	appRepo := &stubAppRepo{}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.QueryLogs(context.Background(), LogQueryOptions{
		Start: "2024-01-01T10:00:00Z",
		End:   "2024-01-01T11:00:00Z",
		Since: "30m", // should be ignored
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expectedStart, _ := time.Parse(time.RFC3339, "2024-01-01T10:00:00Z")
	expectedEnd, _ := time.Parse(time.RFC3339, "2024-01-01T11:00:00Z")
	if !querier.lastQuery.Start.Equal(expectedStart) {
		t.Errorf("start = %v, want %v", querier.lastQuery.Start, expectedStart)
	}
	if !querier.lastQuery.End.Equal(expectedEnd) {
		t.Errorf("end = %v, want %v", querier.lastQuery.End, expectedEnd)
	}
}

func TestQueryLogs_MultiApp(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "any"}}
	querier := &stubLogQuerier{}
	svc := NewLogService(appRepo, querier, "prod")

	_, err := svc.QueryLogs(context.Background(), LogQueryOptions{
		App:   "lark-server,agent-service",
		Since: "1h",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(querier.lastQuery.Apps) != 2 {
		t.Fatalf("expected 2 apps, got %v", querier.lastQuery.Apps)
	}
	if querier.lastQuery.Apps[0] != "lark-server" || querier.lastQuery.Apps[1] != "agent-service" {
		t.Errorf("apps = %v, want [lark-server agent-service]", querier.lastQuery.Apps)
	}
}
