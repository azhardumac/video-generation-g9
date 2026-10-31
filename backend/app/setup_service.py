"""Read-only local setup inspection and explicit bootstrap process control."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from threading import Lock, Thread
from typing import Any

from .config import Settings


LTX_SOURCE_REVISION = "14a3ddd6054736beb45b4d836135fa4d44490531"
LTX_PIPELINES_VERSION = "0.18.0"
PYTHON_VERSION = "3.11.17"
NODE_MAJOR = 22
REMOTION_VERSION = "4.0.538"
REMOTION_REACT_VERSION = "19.2.3"
MODEL_ID = "dgrauet/ltx-2.3-mlx-q8"
STATUS_VALUES = {"installed", "missing", "incompatible", "downloading"}


def _run(args: list[str], timeout: float = 4.0) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return (result.stdout or result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _row(component_id: str, title: str, status: str, detail: str, path: str | None = None) -> dict[str, Any]:
    if status not in STATUS_VALUES:
        raise ValueError(f"Invalid setup status: {status}")
    return {"id": component_id, "title": title, "status": status, "detail": detail, "path": path}


def inspect_setup(settings: Settings, app_root: Path | None = None) -> dict[str, Any]:
    """Inspect executable versions and paths without network calls or file writes."""
    root = (app_root or Path(__file__).resolve().parents[2]).resolve()
    components: list[dict[str, Any]] = []

    brew = shutil.which("brew")
    components.append(_row("homebrew", "Homebrew", "installed" if brew else "missing",
                           _run([brew, "--version"]).splitlines()[0] if brew else "Install Homebrew to manage the local prerequisites.", brew))
    for name, label in (("git", "Git"), ("gh", "GitHub CLI"), ("ffmpeg", "FFmpeg"), ("ffprobe", "ffprobe"), ("uv", "uv")):
        executable = shutil.which(name)
        version = _run([executable, "--version"]).splitlines()[0] if executable else ""
        components.append(_row(name, label, "installed" if executable else "missing",
                               version if executable else f"{label} is not on PATH.", executable))

    brew_python = shutil.which("python3.11")
    brew_python_version = _run([brew_python, "--version"]) if brew_python else ""
    brew_python_ok = brew_python_version.startswith("Python 3.11.")
    components.append(_row("python_homebrew", "Homebrew Python 3.11", "installed" if brew_python_ok else ("missing" if not brew_python else "incompatible"),
                           brew_python_version or "Homebrew Python 3.11 is not on PATH.", brew_python))

    backend_env = root / "backend" / ".venv" / "bin" / "python"
    uv = shutil.which("uv")
    managed_python = _run([uv, "python", "find", PYTHON_VERSION]) if uv else ""
    python_candidate = str(backend_env) if backend_env.is_file() else managed_python
    python_version = _run([python_candidate, "--version"]) if python_candidate else ""
    python_compatible = python_version.rstrip().endswith(PYTHON_VERSION)
    python_status = "installed" if python_compatible else "incompatible" if python_candidate else "missing"
    components.append(_row("python", "Python", python_status,
                           f"Detected {python_version or 'no managed interpreter'}; bootstrap pin is {PYTHON_VERSION}.", python_candidate or None))

    node = shutil.which("node")
    node_version = _run([node, "--version"]) if node else ""
    node_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", node_version)
    node_ok = bool(node_match and int(node_match.group(1)) == NODE_MAJOR)
    components.append(_row("node", "Node.js", "installed" if node_ok else ("missing" if not node else "incompatible"),
                           f"Detected {node_version or 'not installed'}; supported line is Node {NODE_MAJOR} LTS.", node))
    npm = shutil.which("npm")
    components.append(_row("npm", "npm", "installed" if npm and node_ok else ("missing" if not npm else "incompatible"),
                           _run([npm, "--version"]) if npm else "npm is not on PATH.", npm))

    backend_check = _run([str(backend_env), "-c", "import fastapi,uvicorn,pydantic; print(fastapi.__version__)"]) if backend_env.is_file() else ""
    components.append(_row("backend", "FastAPI backend", "installed" if backend_check else "missing",
                           f"FastAPI {backend_check}" if backend_check else "Pinned backend environment is not installed.", str(backend_env)))
    vite = root / "frontend" / "node_modules" / ".bin" / "vite"
    components.append(_row("frontend", "React / Vite frontend", "installed" if vite.is_file() else "missing",
                           "npm lockfile dependencies are present." if vite.is_file() else "Run the bootstrap to install the locked frontend dependencies.", str(vite)))

    remotion_root = settings.expanded(settings.remotion_studio)
    manifest_path = remotion_root / "package.json"
    remotion_cli = remotion_root / "node_modules" / ".bin" / "remotion"
    actual_versions: dict[str, str] = {}
    try:
        manifest = json.loads(manifest_path.read_text())
        for package in ("@remotion/cli", "remotion", "react", "react-dom"):
            installed_manifest = remotion_root / "node_modules" / package / "package.json"
            installed = json.loads(installed_manifest.read_text()).get("version") if installed_manifest.is_file() else None
            actual_versions[package] = installed or "missing"
    except (OSError, ValueError):
        pass
    remotion_present = remotion_cli.is_file() and all(actual_versions.get(package) == expected for package, expected in (
        ("@remotion/cli", REMOTION_VERSION), ("remotion", REMOTION_VERSION),
        ("react", REMOTION_REACT_VERSION), ("react-dom", REMOTION_REACT_VERSION)))
    remotion_incompatible = bool(actual_versions and any(version != "missing" for version in actual_versions.values()) and not remotion_present)
    remotion_status = "installed" if remotion_present else "incompatible" if remotion_incompatible else "missing"
    components.append(_row("remotion", "Remotion", remotion_status,
                           f"CLI/runtime {actual_versions.get('@remotion/cli', 'missing')} / {actual_versions.get('remotion', 'missing')}, React {actual_versions.get('react', 'missing')}; expected {REMOTION_VERSION} / React {REMOTION_REACT_VERSION}.", str(remotion_root)))

    ltx_root = settings.expanded(settings.ltx_root)
    revision = _run(["git", "-C", str(ltx_root), "rev-parse", "HEAD"]) if (ltx_root / ".git").exists() else ""
    ltx_source_status = "installed" if revision == LTX_SOURCE_REVISION else "incompatible" if revision else "missing"
    components.append(_row("ltx_source", "LTX-2.3 MLX source", ltx_source_status,
                           f"Revision {revision or 'not installed'}; expected {LTX_SOURCE_REVISION} (pipeline {LTX_PIPELINES_VERSION}).", str(ltx_root)))
    ltx_cli = settings.expanded(settings.ltx_executable)
    components.append(_row("ltx_runner", "LTX runner", "installed" if ltx_cli.is_file() and os.access(ltx_cli, os.X_OK) else "missing",
                           "Configured CLI entrypoint is executable." if ltx_cli.is_file() and os.access(ltx_cli, os.X_OK) else "Configured CLI entrypoint is missing or not executable.", str(ltx_cli)))

    hf_root = settings.expanded(settings.hf_cache)
    hub_root = hf_root / "hub" if (hf_root / "hub").is_dir() else hf_root
    model_repo = hub_root / "models--dgrauet--ltx-2.3-mlx-q8"
    snapshots = model_repo / "snapshots"
    has_weights = any(snapshots.glob("*/*.safetensors")) if snapshots.is_dir() else False
    components.append(_row("model", "LTX-2.3 MLX Q8 model cache", "installed" if has_weights else "missing",
                           f"{MODEL_ID} {'is present in the local Hugging Face cache.' if has_weights else 'is not complete in the local Hugging Face cache; download is opt-in.'}", str(hf_root)))

    env_path = root / ".env"
    components.append(_row("env", "Local .env configuration", "installed" if env_path.is_file() else "missing",
                           "Existing local configuration will be preserved." if env_path.is_file() else "Bootstrap creates .env from .env.example only when missing.", str(env_path)))
    for folder_id, title, path in (("projects", "Projects directory", settings.expanded(settings.story_project).parent),
                                   ("outputs", "Output directory", settings.expanded(settings.output_root))):
        components.append(_row(folder_id, title, "installed" if path.is_dir() else "missing",
                               "Directory exists; contents are retained." if path.is_dir() else "Directory will be created without replacing existing files.", str(path)))

    app_disk_free = shutil.disk_usage(root).free
    model_disk_path = hf_root
    while not model_disk_path.exists() and model_disk_path != model_disk_path.parent:
        model_disk_path = model_disk_path.parent
    model_disk_free = shutil.disk_usage(model_disk_path).free
    failed = [item for item in components if item["status"] != "installed"]
    return {"components": components, "ready": not failed, "disk_free_bytes": app_disk_free,
            "model_disk_free_bytes": model_disk_free,
            "installing": None, "logs": [], "install_state": "idle", "exit_code": None}


class SetupManager:
    """Own one explicit installer subprocess and retain its recent output for the UI."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=300)
        self._stage: str | None = None
        self._download_progress: float | None = None
        self._exit_code: int | None = None
        self._state = "idle"
        self._include_model = False
        self._cancel_requested = False

    def start(self, app_root: Path, settings: Settings, include_model: bool, confirm_model_download: bool = False) -> dict[str, Any]:
        if include_model and not confirm_model_download:
            raise PermissionError("Explicit model-download confirmation is required")
        script = app_root / "scripts" / "bootstrap.sh"
        if not script.is_file():
            raise FileNotFoundError(f"Setup script is missing: {script}")
        with self._lock:
            if self._process and self._process.poll() is None:
                raise RuntimeError("A setup operation is already running")
            env = os.environ.copy()
            env.update({"AI_VIDEO_STUDIO_HOME": str(settings.allowed_roots[0]), "AI_VIDEO_STUDIO_APP_DIR": str(app_root),
                        "LTX_ROOT": str(settings.expanded(settings.ltx_root)), "HF_CACHE": str(settings.expanded(settings.hf_cache)),
                        "REMOTION_STUDIO": str(settings.expanded(settings.remotion_studio)),
                        "STORY_PROJECT": str(settings.expanded(settings.story_project)),
                        "OUTPUT_ROOT": str(settings.expanded(settings.output_root)), "PYTHONUNBUFFERED": "1"})
            args = [str(script), "--install-missing", "--download-model" if include_model else "--skip-model"]
            self._logs.clear()
            self._stage = "Starting setup"
            self._download_progress = 0.0 if include_model else None
            self._exit_code = None
            self._state = "running"
            self._include_model = include_model
            self._cancel_requested = False
            try:
                self._process = subprocess.Popen(args, cwd=str(app_root), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    bufsize=1, shell=False, start_new_session=(os.name == "posix"))
            except OSError as exc:
                self._state = "failed"
                self._logs.append(f"Could not start setup: {exc}")
                raise
            Thread(target=self._drain, args=(self._process,), daemon=True, name="studio-setup-output").start()
        return self.snapshot()

    def _drain(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip()
            with self._lock:
                self._logs.append(line)
                if line.startswith("SETUP_STAGE:"):
                    self._stage = line.removeprefix("SETUP_STAGE:")
                    if self._include_model and "model" in self._stage.casefold():
                        self._download_progress = 0.0
                match = re.search(r"DOWNLOAD_PROGRESS\s+(\d+)\s+(\d+)", line)
                if match and int(match.group(2)) > 0:
                    self._download_progress = min(100.0, int(match.group(1)) * 100.0 / int(match.group(2)))
        code = process.wait()
        with self._lock:
            if self._process is process:
                self._exit_code = code
                self._state = "cancelled" if self._cancel_requested else "completed" if code == 0 else "failed"
                if code == 0:
                    self._stage = "Setup finished"
                    self._download_progress = 100.0 if self._include_model else None
                else:
                    self._stage = "Setup stopped; rerun to resume from retained downloads and dependencies"

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if not process or process.poll() is not None:
                raise RuntimeError("No setup operation is currently running")
            self._cancel_requested = True
            self._state = "cancelling"
            self._logs.append("Stop requested. The bootstrap process group is stopping; cached partial downloads are retained for resume.")
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._process and self._process.poll() is None)
            return {"installing": self._stage if running else None, "install_state": self._state,
                    "download_progress_percent": self._download_progress, "logs": list(self._logs), "exit_code": self._exit_code}


setup_manager = SetupManager()
