package domain

import (
	"errors"
	"fmt"
)

var (
	ErrNotFound      = errors.New("not found")
	ErrAlreadyExists = errors.New("already exists")
	ErrInvalidInput  = errors.New("invalid input")
	ErrCannotDelete  = errors.New("cannot delete")
	ErrCannotCancel  = errors.New("cannot cancel")

	ErrAppNotFound       = fmt.Errorf("app %w", ErrNotFound)
	ErrBuildNotFound     = fmt.Errorf("build %w", ErrNotFound)
	ErrReleaseNotFound   = fmt.Errorf("release %w", ErrNotFound)
	ErrImageRepoNotFound = fmt.Errorf("image repo %w", ErrNotFound)

	ErrNonMainProdDeploy = fmt.Errorf("%w: prod lane only accepts images built from main branch", ErrInvalidInput)
)
