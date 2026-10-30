from hashlib import sha256
from pathlib import Path

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v", ".webm": "video/webm"}


def classify_asset(relative: Path) -> tuple[str, str] | None:
    ext = relative.suffix.lower()
    parts = [part.casefold() for part in relative.parts]
    if ext in IMAGE_TYPES and "references" in parts:
        tokens = " ".join(parts)
        if any(word in tokens for word in ("character", "characters", "frog", "scorpion")):
            return "character_reference", IMAGE_TYPES[ext]
        if any(word in tokens for word in ("scene", "scenes", "riverbank", "environment")):
            return "scene_reference", IMAGE_TYPES[ext]
        return "reference", IMAGE_TYPES[ext]
    if ext in VIDEO_TYPES:
        if "benchmarks" in parts:
            category = "benchmark_video"
        elif "renders" in parts:
            category = "rendered_video"
        else:
            category = "video"
        return category, VIDEO_TYPES[ext]
    return None


def scan_project(root: Path, max_assets: int = 2000) -> list[dict]:
    assets = []
    for folder, dirs, files in __import__("os").walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(folder) / d).is_symlink())
        for filename in sorted(files):
            if filename.startswith("."):
                continue
            path = Path(folder) / filename
            relative = path.relative_to(root)
            classified = classify_asset(relative)
            if classified is None or path.is_symlink() or not path.is_file():
                continue
            category, mime = classified
            stat = path.stat()
            rel = relative.as_posix()
            assets.append({"id": sha256(rel.encode()).hexdigest()[:20], "category": category,
                           "relative_path": rel, "path": str(path.resolve()), "name": path.name,
                           "size_bytes": stat.st_size, "modified_at": stat.st_mtime, "mime_type": mime})
            if len(assets) >= max_assets:
                return assets
    return assets
