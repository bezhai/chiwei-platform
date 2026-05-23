package repository

import (
	"encoding/json"
	"testing"
)

// TestGatewayRuleToModel_SerializesSplitKeyHeaders: split_key_headers is stored
// as a JSON array string (jsonb column).
func TestGatewayRuleToModel_SerializesSplitKeyHeaders(t *testing.T) {
	rule := sampleGatewayRule()
	rule.SplitKeyHeaders = []string{"X-User-Id", "X-Trace-Id"}
	m, err := gatewayRuleToModel(rule)
	if err != nil {
		t.Fatalf("gatewayRuleToModel failed: %v", err)
	}
	var got []string
	if err := json.Unmarshal([]byte(m.SplitKeyHeaders), &got); err != nil {
		t.Fatalf("split_key_headers not valid JSON: %v (raw=%q)", err, m.SplitKeyHeaders)
	}
	if len(got) != 2 || got[0] != "X-User-Id" || got[1] != "X-Trace-Id" {
		t.Errorf("split_key_headers not preserved: %+v", got)
	}
}

// TestGatewayRuleRoundTrip_SplitKeyHeaders: a configured split list survives
// domain->model->domain.
func TestGatewayRuleRoundTrip_SplitKeyHeaders(t *testing.T) {
	original := sampleGatewayRule()
	original.SplitKeyHeaders = []string{"X-User-Id"}
	m, err := gatewayRuleToModel(original)
	if err != nil {
		t.Fatalf("toModel: %v", err)
	}
	got, err := modelToGatewayRule(m)
	if err != nil {
		t.Fatalf("toDomain: %v", err)
	}
	if len(got.SplitKeyHeaders) != 1 || got.SplitKeyHeaders[0] != "X-User-Id" {
		t.Errorf("split_key_headers round-trip mismatch: %+v", got.SplitKeyHeaders)
	}
}

// TestModelToGatewayRule_SplitKeyHeadersEmpty: an empty/absent jsonb column
// decodes to an empty (nil) slice, not an error.
func TestModelToGatewayRule_SplitKeyHeadersEmpty(t *testing.T) {
	for _, raw := range []string{"", "[]", "null"} {
		m := &GatewayRuleModel{
			Name:            "x",
			PathPrefix:      "/x/",
			Match:           `{"path_prefix":"/x/"}`,
			Targets:         "[]",
			SplitKeyHeaders: raw,
		}
		got, err := modelToGatewayRule(m)
		if err != nil {
			t.Fatalf("raw=%q: modelToGatewayRule failed: %v", raw, err)
		}
		if len(got.SplitKeyHeaders) != 0 {
			t.Errorf("raw=%q: expected no split headers, got %+v", raw, got.SplitKeyHeaders)
		}
	}
}
