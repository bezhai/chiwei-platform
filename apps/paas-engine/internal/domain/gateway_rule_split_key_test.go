package domain

import "testing"

// TestValidateGatewayRule_SplitKeyHeadersEmptyOK: nil and empty split key
// headers are valid (no stable split configured).
func TestValidateGatewayRule_SplitKeyHeadersEmptyOK(t *testing.T) {
	r := validRule()
	r.SplitKeyHeaders = nil
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("nil split_key_headers must pass, got: %v", err)
	}
	r.SplitKeyHeaders = []string{}
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("empty split_key_headers must pass, got: %v", err)
	}
}

// TestValidateGatewayRule_SplitKeyHeadersValid: well-formed header names pass.
func TestValidateGatewayRule_SplitKeyHeadersValid(t *testing.T) {
	r := validRule()
	r.SplitKeyHeaders = []string{"X-User-Id", "X-Trace-Id", "x-lane"}
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("valid header names must pass, got: %v", err)
	}
}

// TestValidateGatewayRule_SplitKeyHeadersRejectEmptyElement: an empty string
// element is meaningless and rejected.
func TestValidateGatewayRule_SplitKeyHeadersRejectEmptyElement(t *testing.T) {
	r := validRule()
	r.SplitKeyHeaders = []string{"X-User-Id", ""}
	assertReject(t, r, "split_key_headers")
}

// TestValidateGatewayRule_SplitKeyHeadersRejectInvalidName: header names with
// spaces, colons, or other non-token characters are rejected.
func TestValidateGatewayRule_SplitKeyHeadersRejectInvalidName(t *testing.T) {
	for _, bad := range []string{"has space", "has:colon", "新建", "tab\there", "with/slash"} {
		r := validRule()
		r.SplitKeyHeaders = []string{bad}
		assertReject(t, r, "split_key_headers")
	}
}
