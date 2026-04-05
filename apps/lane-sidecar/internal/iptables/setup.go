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

// SkipPorts 是不应被 sidecar 拦截的端口（非 HTTP 协议）。
var SkipPorts = []int{
	5432,  // PostgreSQL
	6379,  // Redis
	27017, // MongoDB
	5672,  // RabbitMQ (AMQP)
	15672, // RabbitMQ Management
	443,   // HTTPS (TLS, sidecar 无法解析)
}

func Rules(proxyPort, proxyUID int) [][]string {
	rules := [][]string{
		{"iptables", "-t", "nat", "-N", "LANE_SIDECAR_OUTPUT"},

		// sidecar 自身流量不拦截
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-m", "owner", "--uid-owner", fmt.Sprint(proxyUID), "-j", "RETURN"},

		// localhost 不拦截
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-d", "127.0.0.1/32", "-j", "RETURN"},
	}

	// 跳过非 HTTP 端口（数据库、缓存、MQ、TLS）
	for _, port := range SkipPorts {
		rules = append(rules, []string{
			"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-p", "tcp", "--dport", fmt.Sprint(port), "-j", "RETURN",
		})
	}

	// 其余出站 TCP 重定向到 sidecar
	rules = append(rules, []string{
		"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
		"-p", "tcp", "-j", "REDIRECT", "--to-port", fmt.Sprint(proxyPort),
	})

	// 挂到 OUTPUT 链
	rules = append(rules, []string{
		"iptables", "-t", "nat", "-A", "OUTPUT", "-j", "LANE_SIDECAR_OUTPUT",
	})

	return rules
}

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
