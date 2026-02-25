package service

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
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
	lastNamespace string
	lastAppName   string
	lastLane      string
	lastLimit     int
}

func (s *stubLogQuerier) QueryBuildLogs(_ context.Context, _, _ string, _, _ time.Time) (string, error) {
	return "", nil
}

func (s *stubLogQuerier) QueryAppLogs(_ context.Context, namespace, appName, lane string, _, _ time.Time, limit int) (string, error) {
	s.lastNamespace = namespace
	s.lastAppName = appName
	s.lastLane = lane
	s.lastLimit = limit
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
	if querier.lastNamespace != "prod" {
		t.Errorf("namespace = %q, want prod", querier.lastNamespace)
	}
	if querier.lastLane != "prod" {
		t.Errorf("lane = %q, want prod", querier.lastLane)
	}
	if querier.lastLimit != 500 {
		t.Errorf("limit = %d, want 500", querier.lastLimit)
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
	if querier.lastLane != "" {
		t.Errorf("expected empty lane, got %q", querier.lastLane)
	}
	// limit 默认 1000
	if querier.lastLimit != 1000 {
		t.Errorf("limit = %d, want 1000", querier.lastLimit)
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
	if querier.lastLimit != 5000 {
		t.Errorf("limit should be capped at 5000, got %d", querier.lastLimit)
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
