from __future__ import annotations

import pytest

from app.service.path_validation import PathValidationError, validate_basename_paths


def test_validate_paths_accepts_basename_object_names() -> None:
    validate_basename_paths(["5486389_p0.jpg"], max_batch_paths=2)


def test_validate_paths_rejects_directory_prefixed_keys() -> None:
    with pytest.raises(PathValidationError) as exc_info:
        validate_basename_paths(["pixiv_img_v2/20240202/5486389_p0.jpg"], max_batch_paths=2)

    assert exc_info.value.status_code == 400
    assert "basename" in exc_info.value.detail


def test_validate_paths_rejects_batch_over_limit() -> None:
    with pytest.raises(PathValidationError) as exc_info:
        validate_basename_paths(["1_p0.jpg", "2_p0.jpg", "3_p0.jpg"], max_batch_paths=2)

    assert exc_info.value.status_code == 413
