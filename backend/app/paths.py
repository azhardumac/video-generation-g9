from pathlib import Path

from .config import Settings


def safe_local_path(raw: str, settings: Settings, *, must_exist: bool = False) -> Path:
    candidate = Path(raw).expanduser().resolve(strict=must_exist)
    roots = settings.allowed_roots
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise ValueError("Path must be within ALLOWED_PATH_ROOTS")
    return candidate
