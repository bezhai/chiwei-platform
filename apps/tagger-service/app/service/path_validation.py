from __future__ import annotations


class PathValidationError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def validate_basename_paths(paths: list[str], *, max_batch_paths: int) -> None:
    if len(paths) > max_batch_paths:
        raise PathValidationError(413, f"too many paths: {len(paths)} > {max_batch_paths}")
    if any(not path or "/" in path or path.startswith(".") for path in paths):
        raise PathValidationError(400, "paths must be basename MinIO object names like 5486389_p0.jpg")
