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
func Rules(proxyPort, proxyUID int) [][]string {
	return [][]string{
		// 新建自定义链
		{"iptables", "-t", "nat", "-N", "LANE_SIDECAR_OUTPUT"},

		// sidecar 自身流量不拦截（避免死循环）
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-m", "owner", "--uid-owner", fmt.Sprint(proxyUID), "-j", "RETURN"},

		// localhost 不拦截
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-d", "127.0.0.1/32", "-j", "RETURN"},

		// 所有出站 TCP 重定向到 sidecar（sidecar 做协议检测分流）
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-p", "tcp", "-j", "REDIRECT", "--to-port", fmt.Sprint(proxyPort)},

		// 挂到 OUTPUT 链
		{"iptables", "-t", "nat", "-A", "OUTPUT", "-j", "LANE_SIDECAR_OUTPUT"},
	}
}

// Setup executes the iptables rules.
func Setup(proxyPort, proxyUID int) error {
	for _, args := range Rules(proxyPort, proxyUID) {
		cmd := exec.Command(args[0], args[1:]...)
		out, err := cmd.CombinedOutput()
		if err != nil {
			return fmt.Errorf("failed to run %s: %w\noutput: %s",
				strings.Join(args, " "), err, string(out))
		}
	}
	return nil
}
