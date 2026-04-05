//go:build linux

package proxy

import (
	"fmt"
	"net"
	"syscall"
	"unsafe"
)

// SO_ORIGINAL_DST is the sockopt for retrieving the original destination
// address from an iptables REDIRECT target. Value 80 = SO_ORIGINAL_DST.
const soOriginalDst = 80

// GetOriginalDst retrieves the original destination address of a connection
// that was redirected by iptables REDIRECT. This is used when the Host
// header is unavailable (e.g. non-HTTP traffic).
func GetOriginalDst(conn *net.TCPConn) (net.Addr, error) {
	rawConn, err := conn.SyscallConn()
	if err != nil {
		return nil, fmt.Errorf("get raw conn: %w", err)
	}

	var (
		addr    syscall.RawSockaddrInet4
		addrErr error
	)

	err = rawConn.Control(func(fd uintptr) {
		addrLen := uint32(unsafe.Sizeof(addr))
		_, _, errno := syscall.Syscall6(
			syscall.SYS_GETSOCKOPT,
			fd,
			syscall.SOL_IP,
			soOriginalDst,
			uintptr(unsafe.Pointer(&addr)),
			uintptr(unsafe.Pointer(&addrLen)),
			0,
		)
		if errno != 0 {
			addrErr = fmt.Errorf("getsockopt SO_ORIGINAL_DST: %w", errno)
		}
	})
	if err != nil {
		return nil, fmt.Errorf("raw conn control: %w", err)
	}
	if addrErr != nil {
		return nil, addrErr
	}

	// RawSockaddrInet4.Port is big-endian.
	port := int(addr.Port>>8) | int(addr.Port&0xff)<<8
	ip := net.IPv4(addr.Addr[0], addr.Addr[1], addr.Addr[2], addr.Addr[3])

	return &net.TCPAddr{IP: ip, Port: port}, nil
}
