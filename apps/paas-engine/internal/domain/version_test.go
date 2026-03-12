package domain

import "testing"

func TestParseVersion(t *testing.T) {
	tests := []struct {
		input   string
		want    Version
		wantErr bool
	}{
		{"1.0.0.1", Version{1, 0, 0, 1}, false},
		{"2.3.4.5", Version{2, 3, 4, 5}, false},
		{"0.0.0.0", Version{0, 0, 0, 0}, false},
		{"10.20.30.40", Version{10, 20, 30, 40}, false},
		{"1.0.0", Version{}, true},
		{"1.0.0.0.0", Version{}, true},
		{"a.b.c.d", Version{}, true},
		{"1.0.-1.0", Version{}, true},
		{"", Version{}, true},
	}
	for _, tt := range tests {
		got, err := ParseVersion(tt.input)
		if (err != nil) != tt.wantErr {
			t.Errorf("ParseVersion(%q) error = %v, wantErr %v", tt.input, err, tt.wantErr)
			continue
		}
		if got != tt.want {
			t.Errorf("ParseVersion(%q) = %v, want %v", tt.input, got, tt.want)
		}
	}
}

func TestVersion_String(t *testing.T) {
	tests := []struct {
		v    Version
		want string
	}{
		{Version{1, 0, 0, 1}, "1.0.0.1"},
		{Version{2, 3, 4, 5}, "2.3.4.5"},
		{Version{0, 0, 0, 0}, "0.0.0.0"},
	}
	for _, tt := range tests {
		if got := tt.v.String(); got != tt.want {
			t.Errorf("%v.String() = %q, want %q", tt.v, got, tt.want)
		}
	}
}

func TestVersion_IsZero(t *testing.T) {
	zero := Version{}
	if !zero.IsZero() {
		t.Error("zero value should be zero")
	}
	nonZero := Version{1, 0, 0, 1}
	if nonZero.IsZero() {
		t.Error("1.0.0.1 should not be zero")
	}
}

func TestVersion_Compare(t *testing.T) {
	tests := []struct {
		a, b Version
		want int
	}{
		{Version{1, 0, 0, 1}, Version{1, 0, 0, 1}, 0},
		{Version{1, 0, 0, 1}, Version{1, 0, 0, 2}, -1},
		{Version{1, 0, 0, 2}, Version{1, 0, 0, 1}, 1},
		{Version{1, 0, 1, 1}, Version{1, 0, 0, 99}, 1},
		{Version{1, 1, 0, 1}, Version{1, 0, 99, 99}, 1},
		{Version{2, 0, 0, 1}, Version{1, 99, 99, 99}, 1},
		{Version{1, 0, 0, 0}, Version{2, 0, 0, 0}, -1},
	}
	for _, tt := range tests {
		if got := tt.a.Compare(tt.b); got != tt.want {
			t.Errorf("%v.Compare(%v) = %d, want %d", tt.a, tt.b, got, tt.want)
		}
	}
}

func TestVersion_Next(t *testing.T) {
	tests := []struct {
		v    Version
		bump string
		want Version
	}{
		// 零值 → 初始版本
		{Version{}, "", Version{1, 0, 0, 1}},
		// build 递增
		{Version{1, 0, 0, 1}, "", Version{1, 0, 0, 2}},
		{Version{1, 0, 0, 5}, "", Version{1, 0, 0, 6}},
		// patch bump
		{Version{1, 0, 0, 5}, "patch", Version{1, 0, 1, 1}},
		{Version{1, 0, 2, 5}, "patch", Version{1, 0, 3, 1}},
		// minor bump
		{Version{1, 0, 2, 5}, "minor", Version{1, 1, 0, 1}},
		{Version{1, 2, 3, 5}, "minor", Version{1, 3, 0, 1}},
		// major bump
		{Version{1, 2, 3, 5}, "major", Version{2, 0, 0, 1}},
		{Version{0, 0, 0, 0}, "major", Version{1, 0, 0, 1}},
	}
	for _, tt := range tests {
		if got := tt.v.Next(tt.bump); got != tt.want {
			t.Errorf("%v.Next(%q) = %v, want %v", tt.v, tt.bump, got, tt.want)
		}
	}
}

func TestResolveChannel(t *testing.T) {
	tests := []struct {
		gitRef string
		want   string
	}{
		{"main", ChannelStable},
		{"develop", ChannelTest},
		{"feature/foo", ChannelTest},
		{"v1.0.0", ChannelTest},
	}
	for _, tt := range tests {
		if got := ResolveChannel(tt.gitRef); got != tt.want {
			t.Errorf("ResolveChannel(%q) = %q, want %q", tt.gitRef, got, tt.want)
		}
	}
}
