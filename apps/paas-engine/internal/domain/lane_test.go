package domain

import "testing"

func TestClassifyLane(t *testing.T) {
	cases := []struct {
		name      string
		lane      string
		whitelist []string
		want      LaneClass
		wantErr   bool
	}{
		{name: "prod 保留名", lane: "prod", want: LaneClassProd},
		{name: "blue 保留名", lane: "blue", want: LaneClassProd},
		{name: "coe 前缀", lane: "coe-test-1", want: LaneClassCoe},
		{name: "ppe 前缀", lane: "ppe-canary", want: LaneClassPpe},
		{name: "coe 前缀但只有前缀字面 reject", lane: "coe-", wantErr: true},
		{name: "ppe 前缀但只有前缀字面 reject", lane: "ppe-", wantErr: true},
		{name: "无前缀 reject", lane: "feature-x", wantErr: true},
		{name: "无前缀 reject (sandbox)", lane: "sandbox", wantErr: true},
		{name: "白名单兼容 dev", lane: "dev", whitelist: []string{"dev"}, want: LaneClassProd},
		{name: "白名单不在 reject", lane: "weird-old-lane", whitelist: []string{"dev"}, wantErr: true},
		{name: "空 lane reject", lane: "", wantErr: true},
		{name: "大写 reject (强制小写)", lane: "Coe-Foo", wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ClassifyLane(tc.lane, tc.whitelist)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got class=%v", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Fatalf("class=%v, want=%v", got, tc.want)
			}
		})
	}
}
