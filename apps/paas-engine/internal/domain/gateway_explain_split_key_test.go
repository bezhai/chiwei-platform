package domain

import "testing"

// TestExplainReportsStableSplit: when the winning rule has split_key_headers
// configured, explain reports StableSplit=true and surfaces the header list;
// otherwise StableSplit=false.
func TestExplainReportsStableSplit(t *testing.T) {
	withSplit := gwRule("agent", "/api/agent/", "", 100, true, tgt("agent-service", "", 8000, 100))
	withSplit.SplitKeyHeaders = []string{"X-User-Id", "X-Trace-Id"}
	res := ExplainGatewayMatch([]*GatewayRule{withSplit}, "/api/agent/health", "")
	if !res.Matched {
		t.Fatal("expected match")
	}
	if !res.StableSplit {
		t.Error("expected StableSplit=true for rule with split_key_headers")
	}
	if len(res.SplitKeyHeaders) != 2 || res.SplitKeyHeaders[0] != "X-User-Id" {
		t.Errorf("split_key_headers not surfaced: %+v", res.SplitKeyHeaders)
	}

	noSplit := gwRule("agent", "/api/agent/", "", 100, true, tgt("agent-service", "", 8000, 100))
	res = ExplainGatewayMatch([]*GatewayRule{noSplit}, "/api/agent/health", "")
	if res.StableSplit {
		t.Error("expected StableSplit=false for rule without split_key_headers")
	}
	if len(res.SplitKeyHeaders) != 0 {
		t.Errorf("expected no split_key_headers, got %+v", res.SplitKeyHeaders)
	}
}
