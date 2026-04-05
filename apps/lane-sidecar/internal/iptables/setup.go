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

func Rules(proxyPort, proxyUID int) [][]string {
	return [][]string{
		{"iptables", "-t", "nat", "-N", "LANE_SIDECAR_OUTPUT"},
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-m", "owner", "--uid-owner", fmt.Sprint(proxyUID), "-j", "RETURN"},
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-d", "127.0.0.1/32", "-j", "RETURN"},
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-p", "tcp", "-j", "REDIRECT", "--to-port", fmt.Sprint(proxyPort)},
		{"iptables", "-t", "nat", "-A", "OUTPUT", "-j", "LANE_SIDECAR_OUTPUT"},
	}
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
