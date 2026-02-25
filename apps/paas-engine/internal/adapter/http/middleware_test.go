package http

import (
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
