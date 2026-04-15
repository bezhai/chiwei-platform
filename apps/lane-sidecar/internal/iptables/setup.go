package iptables

import (
	"fmt"
	"os/exec"
	"strings"
)

const (
	ProxyUID  = 1337
	ProxyPort = 15001
)

// Rules generates iptables NAT rules that redirect all outbound TCP traffic
// to the sidecar proxy. The sidecar itself detects the protocol: HTTP traffic
// gets lane-aware routing, non-HTTP traffic is passed through via TCP tunnel.
//
// excludeCIDRs specifies destination CIDRs to skip (e.g., HTTP proxy IPs that
// must be reachable directly without sidecar interception).
func Rules(proxyPort, proxyUID int, excludeCIDRs []string) [][]string {
	rules := [][]string{
		// 新建自定义链
		{"iptables", "-t", "nat", "-N", "LANE_SIDECAR_OUTPUT"},

		// sidecar 自身流量不拦截（避免死循环）
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-m", "owner", "--uid-owner", fmt.Sprint(proxyUID), "-j", "RETURN"},

		// localhost 不拦截
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-d", "127.0.0.1/32", "-j", "RETURN"},
	}

	// 排除指定 CIDR（如 HTTP proxy 地址）
	for _, cidr := range excludeCIDRs {
		cidr = strings.TrimSpace(cidr)
		if cidr == "" {
			continue
		}
		// 无 /mask 的裸 IP 自动补 /32
		if !strings.Contains(cidr, "/") {
			cidr += "/32"
		}
		rules = append(rules, []string{
			"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-d", cidr, "-j", "RETURN",
		})
	}

	rules = append(rules,
		// 所有出站 TCP 重定向到 sidecar（sidecar 做协议检测分流）
		[]string{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-p", "tcp", "-j", "REDIRECT", "--to-port", fmt.Sprint(proxyPort)},

		// 挂到 OUTPUT 链
		[]string{"iptables", "-t", "nat", "-A", "OUTPUT", "-j", "LANE_SIDECAR_OUTPUT"},
	)

	return rules
}

// Setup executes the iptables rules.
func Setup(proxyPort, proxyUID int, excludeCIDRs []string) error {
	for _, args := range Rules(proxyPort, proxyUID, excludeCIDRs) {
		cmd := exec.Command(args[0], args[1:]...)
		out, err := cmd.CombinedOutput()
		if err != nil {
			return fmt.Errorf("failed to run %s: %w\noutput: %s",
				strings.Join(args, " "), err, string(out))
		}
	}
	return nil
}
