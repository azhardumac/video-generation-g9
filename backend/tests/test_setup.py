import os
import importlib.util
from pathlib import Path
import subprocess
import time

import pytest

from app.config import Settings
from app import setup_service
from app.setup_service import SetupManager, _row

_SUPPORT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_support.py"
_SUPPORT_SPEC = importlib.util.spec_from_file_location("bootstrap_support", _SUPPORT_PATH)
assert _SUPPORT_SPEC and _SUPPORT_SPEC.loader
_SUPPORT = importlib.util.module_from_spec(_SUPPORT_SPEC)
_SUPPORT_SPEC.loader.exec_module(_SUPPORT)
write_env_if_missing = _SUPPORT.write_env_if_missing


def _test_settings(root: Path) -> Settings:
    return Settings(_env_file=None, allowed_path_roots=str(root), output_root=str(root / "outputs"),
                    ltx_root=str(root / "ltx"), hf_cache=str(root / "hf"),
                    remotion_studio=str(root / "remotion"), story_project=str(root / "projects" / "story"))


def _fake_app(tmp_path: Path, body: str) -> Path:
    app = tmp_path / "studio app"
    script = app / "scripts" / "bootstrap.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    script.chmod(0o755)
    return app


def _wait(manager: SetupManager, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    state = manager.snapshot()
    while state["install_state"] in ("running", "cancelling") and time.monotonic() < deadline:
        time.sleep(0.02)
        state = manager.snapshot()
    return state


def test_setup_status_values_are_limited_to_real_component_states():
    assert _row("ffmpeg", "FFmpeg", "missing", "not on PATH")["status"] == "missing"
    with pytest.raises(ValueError):
        _row("ffmpeg", "FFmpeg", "ready", "not a supported status")


def test_setup_inspection_is_local_read_only_and_reports_component_states(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_service.shutil, "which", lambda _: None)
    result = setup_service.inspect_setup(_test_settings(tmp_path), app_root=tmp_path)
    assert result["ready"] is False
    assert result["components"]
    assert all(item["status"] in setup_service.STATUS_VALUES for item in result["components"])
    assert next(item for item in result["components"] if item["id"] == "model")["status"] == "missing"
    assert not (tmp_path / ".env").exists()


def test_setup_manager_uses_argv_and_reports_progress_for_explicit_model_opt_in(tmp_path):
    app = _fake_app(tmp_path, "printf 'SETUP_STAGE:model:Downloading\n'\nprintf 'DOWNLOAD_PROGRESS 25 100\n'\nprintf 'ARGS:%s\n' \"$*\"\n")
    manager = SetupManager()
    state = manager.start(app, _test_settings(tmp_path / "install root"), include_model=True, confirm_model_download=True)
    assert state["install_state"] == "running"
    result = _wait(manager)
    assert result["install_state"] == "completed"
    assert result["download_progress_percent"] == 100
    assert any("ARGS:--install-missing --download-model" in line for line in result["logs"])
    assert any("DOWNLOAD_PROGRESS 25 100" in line for line in result["logs"])


def test_setup_manager_rejects_model_download_without_explicit_confirmation(tmp_path):
    app = _fake_app(tmp_path, "exit 0\n")
    with pytest.raises(PermissionError, match="Explicit model-download confirmation"):
        SetupManager().start(app, _test_settings(tmp_path), include_model=True)


def test_setup_manager_skips_model_without_explicit_opt_in(tmp_path):
    app = _fake_app(tmp_path, "printf 'ARGS:%s\n' \"$*\"\n")
    manager = SetupManager()
    manager.start(app, _test_settings(tmp_path), include_model=False)
    result = _wait(manager)
    assert result["install_state"] == "completed"
    assert result["download_progress_percent"] is None
    assert any("ARGS:--install-missing --skip-model" in line for line in result["logs"])


def test_setup_cancel_terminates_only_its_process_group(tmp_path):
    child_file = tmp_path / "child.pid"
    app = _fake_app(tmp_path, f"sleep 30 &\nchild=$!\nprintf '%s\\n' \"$child\" > '{child_file}'\nwait\n")
    manager = SetupManager()
    manager.start(app, _test_settings(tmp_path), include_model=False)
    deadline = time.monotonic() + 2
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_file.exists()
    child_pid = int(child_file.read_text())
    manager.cancel()
    result = _wait(manager)
    assert result["install_state"] == "cancelled"
    assert any("Stop requested" in line for line in result["logs"])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_env_bootstrap_is_exclusive_and_preserves_manual_values(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("API_HOST=127.0.0.1\nLTX_ROOT=~/AI-Video-Studio/src/ltx-2-mlx\n")
    destination = tmp_path / ".env"
    assert write_env_if_missing(template, destination, {"LTX_ROOT":"/path/with spaces/ltx"}) is True
    first_value = destination.read_text()
    assert 'LTX_ROOT="/path/with spaces/ltx"' in first_value
    assert destination.stat().st_mode & 0o777 == 0o600
    destination.write_text("LTX_ROOT=manually-edited\n")
    assert write_env_if_missing(template, destination, {"LTX_ROOT":"replacement"}) is False
    assert destination.read_text() == "LTX_ROOT=manually-edited\n"


def test_bootstrap_reads_local_path_values_without_sourcing_or_printing_secrets(tmp_path):
    app = tmp_path / "existing app"
    app.mkdir()
    (app / ".env").write_text('LTX_ROOT="/custom path/ltx"\nALLOWED_PATH_ROOTS="/custom root"\nAPI_TOKEN=never-print-this\n')
    script = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap.sh"
    environment = os.environ.copy()
    environment.update({"AI_VIDEO_STUDIO_APP_DIR":str(app), "HOME":str(tmp_path / "home")})
    completed = subprocess.run([str(script), "--check"], capture_output=True, text=True, env=environment, check=False)
    assert completed.returncode == 0
    assert "/custom path/ltx" in completed.stdout
    assert "never-print-this" not in completed.stdout


def test_model_helper_check_never_downloads_or_creates_cache(tmp_path):
    helper = Path(__file__).resolve().parents[2] / "scripts" / "cache_ltx_model.py"
    cache = tmp_path / "empty cache"
    completed = subprocess.run([os.environ.get("PYTHON", "python3"), str(helper), "--cache-dir", str(cache), "--check"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 2
    assert "no network request was made" in completed.stdout
    assert not cache.exists()
