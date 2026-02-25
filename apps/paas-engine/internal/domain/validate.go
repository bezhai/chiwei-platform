package domain

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// k8sNameRegex 匹配合法的 K8s 资源名称：小写字母开头，只含小写字母、数字和连字符，长度 2-63。
var k8sNameRegex = regexp.MustCompile(`^[a-z][a-z0-9-]{0,61}[a-z0-9]$`)

// ValidateK8sName 校验名称是否可安全用作 K8s 资源名。
func ValidateK8sName(name string) error {
	if !k8sNameRegex.MatchString(name) {
		return fmt.Errorf("%w: name %q is not a valid k8s resource name", ErrInvalidInput, name)
	}
	return nil
}

// gitRefRegex 白名单：字母、数字、-、_、.、/
var gitRefRegex = regexp.MustCompile(`^[a-zA-Z0-9._/-]+$`)

// ValidateGitRepo 校验 Git 仓库地址，只允许 https:// 或 git:// 协议，防止 SSRF。
func ValidateGitRepo(repo string) error {
	if repo == "" {
		return fmt.Errorf("%w: git_repo is required", ErrInvalidInput)
	}
	if !strings.HasPrefix(repo, "https://") && !strings.HasPrefix(repo, "git://") {
		return fmt.Errorf("%w: git_repo must use https:// or git:// protocol", ErrInvalidInput)
	}
	return nil
}

// ValidateGitRef 校验 Git 引用（branch/tag/commit），使用字符白名单。
func ValidateGitRef(ref string) error {
	if ref == "" {
		return nil // 空值由调用方设默认值
	}
	if !gitRefRegex.MatchString(ref) {
		return fmt.Errorf("%w: git_ref %q contains invalid characters", ErrInvalidInput, ref)
	}
	return nil
}

// contextDirRegex 白名单：字母、数字、-、_、.、/，不允许以 / 开头。
var contextDirRegex = regexp.MustCompile(`^[a-zA-Z0-9._][a-zA-Z0-9._/-]*$`)

// ValidateContextDir 校验上下文子目录，防止路径穿越。
func ValidateContextDir(dir string) error {
	if dir == "" || dir == "." {
		return nil
	}
	if !contextDirRegex.MatchString(dir) {
		return fmt.Errorf("%w: context_dir %q contains invalid characters", ErrInvalidInput, dir)
	}
	// 防止路径穿越
	if strings.Contains(dir, "..") {
		return fmt.Errorf("%w: context_dir %q must not contain '..'", ErrInvalidInput, dir)
	}
	// 清理后不应以 / 开头（绝对路径）
	cleaned := filepath.Clean(dir)
	if filepath.IsAbs(cleaned) {
		return fmt.Errorf("%w: context_dir must be a relative path", ErrInvalidInput)
	}
	return nil
}
