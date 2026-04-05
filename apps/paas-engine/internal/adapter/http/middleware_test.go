package http

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAuthMiddleware(t *testing.T) {
	okHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	tests := []struct {
		name       string
		token      string
		header     string
		wantStatus int
	}{
		{"no token configured, no header", "", "", http.StatusOK},
		{"no token configured, header sent", "", "anything", http.StatusOK},
		{"token configured, correct header", "secret", "secret", http.StatusOK},
		{"token configured, wrong header", "secret", "wrong", http.StatusUnauthorized},
		{"token configured, empty header", "secret", "", http.StatusUnauthorized},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			mw := authMiddleware(tt.token)
			handler := mw(okHandler)

			req := httptest.NewRequest(http.MethodGet, "/", nil)
			if tt.header != "" {
				req.Header.Set("X-API-Key", tt.header)
			}
			rec := httptest.NewRecorder()

			handler.ServeHTTP(rec, req)

			if rec.Code != tt.wantStatus {
				t.Errorf("got status %d, want %d", rec.Code, tt.wantStatus)
			}
		})
	}
}

func TestContextPropagationMiddleware(t *testing.T) {
	handler := contextPropagationMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		headers := GetContextHeaders(r.Context())
		if headers["x-ctx-lane"] != "dev" {
			t.Errorf("expected x-ctx-lane=dev, got %q", headers["x-ctx-lane"])
		}
		if headers["x-ctx-trace-id"] != "abc" {
			t.Errorf("expected x-ctx-trace-id=abc, got %q", headers["x-ctx-trace-id"])
		}
		if _, ok := headers["x-unrelated"]; ok {
			t.Error("unexpected non-ctx header in context")
		}
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("x-ctx-lane", "dev")
	req.Header.Set("x-ctx-trace-id", "abc")
	req.Header.Set("x-unrelated", "ignored")
	w := httptest.NewRecorder()

	handler.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
}

func TestGetContextHeaders_NoContext(t *testing.T) {
	ctx := context.Background()
	headers := GetContextHeaders(ctx)
	if len(headers) != 0 {
		t.Errorf("expected empty headers, got %v", headers)
	}
}
