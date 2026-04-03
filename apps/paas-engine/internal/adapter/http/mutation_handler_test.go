package http

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/adapter/repository"
	"github.com/go-chi/chi/v5"
)

// fakeMutationStore 是 MutationStore 的内存实现，用于单元测试。
type fakeMutationStore struct {
	mutations map[uint]*repository.DbMutationModel
	nextID    uint
}

func newFakeMutationStore() *fakeMutationStore {
	return &fakeMutationStore{
		mutations: make(map[uint]*repository.DbMutationModel),
		nextID:    1,
	}
}

func (f *fakeMutationStore) Create(ctx context.Context, m *repository.DbMutationModel) error {
	m.ID = f.nextID
	f.nextID++
	cp := *m
	f.mutations[cp.ID] = &cp
	return nil
}

func (f *fakeMutationStore) List(ctx context.Context, status string) ([]repository.DbMutationModel, error) {
	var result []repository.DbMutationModel
	for _, m := range f.mutations {
		if status == "" || m.Status == status {
			result = append(result, *m)
		}
	}
	return result, nil
}

func (f *fakeMutationStore) Get(ctx context.Context, id uint) (*repository.DbMutationModel, error) {
	m, ok := f.mutations[id]
	if !ok {
		return nil, errors.New("record not found")
	}
	cp := *m
	return &cp, nil
}

func (f *fakeMutationStore) UpdateStatus(ctx context.Context, id uint, status, reviewedBy, reviewNote string, executedAt *time.Time, execErr string) error {
	m, ok := f.mutations[id]
	if !ok {
		return errors.New("record not found")
	}
	m.Status = status
	m.ReviewedBy = reviewedBy
	m.ReviewNote = reviewNote
	m.ExecutedAt = executedAt
	m.Error = execErr
	return nil
}

func TestSubmitMutation_MissingSQL(t *testing.T) {
	h := NewOpsHandler(nil, nil, newFakeMutationStore())
	r := chi.NewRouter()
	r.Post("/mutations", h.SubmitMutation)

	body, _ := json.Marshal(map[string]string{"db": "chiwei"})
	req := httptest.NewRequest(http.MethodPost, "/mutations", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("want 400, got %d", rec.Code)
	}
}

func TestListMutations_FilterByStatus(t *testing.T) {
	store := newFakeMutationStore()
	ctx := context.Background()
	_ = store.Create(ctx, &repository.DbMutationModel{DB: "chiwei", SQL: "SELECT 1", Status: "pending", SubmittedBy: "claude-code"})
	_ = store.Create(ctx, &repository.DbMutationModel{DB: "chiwei", SQL: "SELECT 2", Status: "approved", SubmittedBy: "claude-code"})

	h := NewOpsHandler(nil, nil, store)
	r := chi.NewRouter()
	r.Get("/mutations", h.ListMutations)

	req := httptest.NewRequest(http.MethodGet, "/mutations?status=pending", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("want 200, got %d", rec.Code)
	}

	var envelope struct {
		Data []map[string]any `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("failed to unmarshal: %v, body: %s", err, rec.Body.String())
	}
	if len(envelope.Data) != 1 {
		t.Errorf("want 1 pending mutation, got %d", len(envelope.Data))
	}
}

func TestGetMutation_NotFound(t *testing.T) {
	h := NewOpsHandler(nil, nil, newFakeMutationStore())
	r := chi.NewRouter()
	r.Get("/mutations/{id}", h.GetMutation)

	req := httptest.NewRequest(http.MethodGet, "/mutations/999", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("want 404, got %d", rec.Code)
	}
}
