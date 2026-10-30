from pathlib import Path
import shutil

from .config import Settings
from .paths import safe_local_path


def _exists(path: str, settings: Settings, *, is_file: bool = False) -> bool:
    try:
        candidate = safe_local_path(path, settings)
        return candidate.is_file() if is_file else candidate.is_dir()
    except (ValueError, OSError):
        return False


def detect_integrations(settings: Settings) -> dict:
    ltx_root = settings.expanded(settings.ltx_root)
    runner = settings.expanded(settings.ltx_executable)
    cache = settings.expanded(settings.hf_cache)
    hf_hub_cache = cache / "hub" if (cache / "hub").is_dir() else cache
    q8_repo = hf_hub_cache / "models--dgrauet--ltx-2.3-mlx-q8"
    # Only inspect the model's standard Hugging Face snapshot folder. Never fetch or modify files.
    cached_weights = False
    snapshots = q8_repo / "snapshots"
    if snapshots.is_dir():
        for snapshot in snapshots.iterdir():
            if snapshot.is_dir() and any(snapshot.glob("*.safetensors")):
                cached_weights = True
                break
    remotion_root = settings.expanded(settings.remotion_studio)
    remotion_cli = settings.expanded(settings.remotion_executable)
    remotion_pkg = remotion_root / "package.json"
    remotion_version = None
    if remotion_pkg.is_file():
        try:
            import json
            pkg = json.loads(remotion_pkg.read_text())
            remotion_version = pkg.get("dependencies", {}).get("@remotion/cli")
        except (OSError, ValueError):
            pass
    ffprobe = shutil.which(settings.ffprobe_executable)
    return {
        "ltx": {"root": str(ltx_root), "installed": _exists(str(ltx_root), settings),
                "runner": str(runner), "runner_available": _exists(str(runner), settings, is_file=True),
                "model": "dgrauet/ltx-2.3-mlx-q8", "hf_home": str(cache), "hf_hub_cache": str(hf_hub_cache),
                "cache_available": _exists(str(cache), settings), "weights_cached": cached_weights,
                "ready": _exists(str(ltx_root), settings) and _exists(str(runner), settings, is_file=True) and cached_weights},
        "remotion": {"root": str(remotion_root), "installed": _exists(str(remotion_root), settings),
                     "cli": str(remotion_cli), "cli_available": _exists(str(remotion_cli), settings, is_file=True),
                     "version": remotion_version,
                     "ready": _exists(str(remotion_root), settings) and _exists(str(remotion_cli), settings, is_file=True)},
        "story_project": {"root": str(settings.expanded(settings.story_project)),
                          "available": _exists(settings.story_project, settings)},
        "ffprobe": {"available": ffprobe is not None, "path": ffprobe},
    }
