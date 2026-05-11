package domain

import (
	"fmt"
	"regexp"
	"slices"
)

// LaneClass 表示 lane 的环境类别。fail-closed：未知类别一律 reject。
type LaneClass int

const (
	LaneClassUnknown LaneClass = iota
	LaneClassProd              // prod / blue / 历史白名单：连 prod 基建
	LaneClassCoe               // coe-*：连测试基建
	LaneClassPpe               // ppe-*：连 prod 基建（灰度/AB）
)

func (c LaneClass) String() string {
	switch c {
	case LaneClassProd:
		return "prod"
	case LaneClassCoe:
		return "coe"
	case LaneClassPpe:
		return "ppe"
	default:
		return "unknown"
	}
}

// 强制 lowercase + 字母数字+ - 之后必须有非空字符。
var coePattern = regexp.MustCompile(`^coe-[a-z0-9][a-z0-9-]*$`)
var ppePattern = regexp.MustCompile(`^ppe-[a-z0-9][a-z0-9-]*$`)

// 保留名（paas-engine 蓝绿专用，等同 prod 基建）。
var reservedNames = []string{"prod", "blue"}

// ClassifyLane 用 fail-closed 语义解析 lane 类别。
//   - prod / blue：保留名，返回 LaneClassProd
//   - coe-* / ppe-*：合法前缀，返回对应类别
//   - whitelist 内：兼容历史 lane，按 LaneClassProd 处理（白名单有过期日期，调用方传入）
//   - 其他：返回 error，caller 必须 reject
func ClassifyLane(lane string, whitelist []string) (LaneClass, error) {
	if lane == "" {
		return LaneClassUnknown, fmt.Errorf("lane name is empty")
	}
	if slices.Contains(reservedNames, lane) {
		return LaneClassProd, nil
	}
	if slices.Contains(whitelist, lane) {
		return LaneClassProd, nil
	}
	if coePattern.MatchString(lane) {
		return LaneClassCoe, nil
	}
	if ppePattern.MatchString(lane) {
		return LaneClassPpe, nil
	}
	return LaneClassUnknown, fmt.Errorf(
		"lane %q rejected: must match prod | blue | coe-<name> | ppe-<name>; got no recognized prefix",
		lane,
	)
}
