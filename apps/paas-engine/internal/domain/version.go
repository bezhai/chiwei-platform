package domain

import (
	"fmt"
	"strconv"
	"strings"
)

const (
	ChannelStable = "stable"
	ChannelTest   = "test"
)

// Version 是 major.minor.patch.build 语义化版本号。
type Version struct {
	Major, Minor, Patch, Build int
}

// ParseVersion 将 "1.0.0.2" 解析为 Version{1,0,0,2}。
func ParseVersion(s string) (Version, error) {
	parts := strings.Split(s, ".")
	if len(parts) != 4 {
		return Version{}, fmt.Errorf("invalid version %q: expected major.minor.patch.build", s)
	}
	nums := make([]int, 4)
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil {
			return Version{}, fmt.Errorf("invalid version %q: %w", s, err)
		}
		if n < 0 {
			return Version{}, fmt.Errorf("invalid version %q: negative number", s)
		}
		nums[i] = n
	}
	return Version{Major: nums[0], Minor: nums[1], Patch: nums[2], Build: nums[3]}, nil
}

// String 返回 "major.minor.patch.build" 格式。
func (v Version) String() string {
	return fmt.Sprintf("%d.%d.%d.%d", v.Major, v.Minor, v.Patch, v.Build)
}

// IsZero 判断是否零值。
func (v Version) IsZero() bool {
	return v.Major == 0 && v.Minor == 0 && v.Patch == 0 && v.Build == 0
}

// Compare 返回 -1、0、1，表示 v 与 other 的大小关系。
func (v Version) Compare(other Version) int {
	pairs := [][2]int{
		{v.Major, other.Major},
		{v.Minor, other.Minor},
		{v.Patch, other.Patch},
		{v.Build, other.Build},
	}
	for _, p := range pairs {
		if p[0] < p[1] {
			return -1
		}
		if p[0] > p[1] {
			return 1
		}
	}
	return 0
}

// Next 根据 bump 类型计算下一版本。
// bump=""    → build+1（零值特殊处理为 1.0.0.1）
// bump="patch" → patch+1, build=1
// bump="minor" → minor+1, patch=0, build=1
// bump="major" → major+1, minor=0, patch=0, build=1
func (v Version) Next(bump string) Version {
	if v.IsZero() && bump == "" {
		return Version{1, 0, 0, 1}
	}
	switch bump {
	case "major":
		return Version{v.Major + 1, 0, 0, 1}
	case "minor":
		return Version{v.Major, v.Minor + 1, 0, 1}
	case "patch":
		return Version{v.Major, v.Minor, v.Patch + 1, 1}
	default:
		return Version{v.Major, v.Minor, v.Patch, v.Build + 1}
	}
}

// ResolveChannel 根据 git ref 决定 channel。main → stable，其余 → test。
func ResolveChannel(gitRef string) string {
	if gitRef == "main" {
		return ChannelStable
	}
	return ChannelTest
}
