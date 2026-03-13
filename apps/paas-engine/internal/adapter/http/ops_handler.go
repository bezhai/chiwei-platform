package http

import (
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"gorm.io/gorm"
)

var writeKeywordRE = regexp.MustCompile(
	`(?i)\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b`,
)

type OpsHandler struct {
	dbs map[string]*gorm.DB // db alias → read-only connection
}

func NewOpsHandler(dbs map[string]*gorm.DB) *OpsHandler {
	return &OpsHandler{dbs: dbs}
}

type opsQueryRequest struct {
	DB  string `json:"db"`
	SQL string `json:"sql"`
}

type opsQueryResponse struct {
	Columns []string `json:"columns"`
	Rows    [][]any  `json:"rows"`
}

func (h *OpsHandler) Query(w http.ResponseWriter, r *http.Request) {
	var req opsQueryRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}

	req.SQL = strings.TrimSpace(req.SQL)
	if req.SQL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sql is required"})
		return
	}

	// Safety: reject write operations
	if writeKeywordRE.MatchString(req.SQL) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "write operations are not allowed"})
		return
	}

	// Resolve database
	dbAlias := req.DB
	if dbAlias == "" {
		dbAlias = "paas_engine"
	}
	db, ok := h.dbs[dbAlias]
	if !ok {
		available := make([]string, 0, len(h.dbs))
		for k := range h.dbs {
			available = append(available, k)
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": fmt.Sprintf("unknown database %q, available: %s", dbAlias, strings.Join(available, ", ")),
		})
		return
	}

	// Execute read-only query
	rows, err := db.WithContext(r.Context()).Raw(req.SQL).Rows()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	defer rows.Close()

	columns, err := rows.Columns()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	var result [][]any
	for rows.Next() {
		values := make([]any, len(columns))
		ptrs := make([]any, len(columns))
		for i := range values {
			ptrs[i] = &values[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		// Convert []byte to string for JSON serialization
		row := make([]any, len(values))
		for i, v := range values {
			if b, ok := v.([]byte); ok {
				row[i] = string(b)
			} else {
				row[i] = v
			}
		}
		result = append(result, row)
	}

	if err := rows.Err(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, opsQueryResponse{
		Columns: columns,
		Rows:    result,
	})
}
