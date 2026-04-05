//go:build !linux

package proxy

import (
	"errors"
	"net"
)

// GetOriginalDst is a stub for non-Linux platforms.
// SO_ORIGINAL_DST is a Linux-only iptables feature.
func GetOriginalDst(conn *net.TCPConn) (net.Addr, error) {
	return nil, errors.New("GetOriginalDst: not supported on this platform")
}
