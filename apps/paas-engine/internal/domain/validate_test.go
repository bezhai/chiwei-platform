package domain

import (
	"testing"
)

func TestValidateContextDir(t *testing.T) {
	tests := []struct {
		dir     string
		wantErr bool
	}{
		{"", false},
		{".", false},
		{"apps/myservice", false},
		{"src", false},
		{"apps/my-service_v2", false},
		{"..", true},
		{"apps/../etc", true},
		{"/etc/passwd", true},
		{"apps/../../secret", true},
		{"apps/ space", true},
	}
	for _, tt := range tests {
		err := ValidateContextDir(tt.dir)
		if (err != nil) != tt.wantErr {
			t.Errorf("ValidateContextDir(%q) error = %v, wantErr %v", tt.dir, err, tt.wantErr)
		}
	}
}
