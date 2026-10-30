import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from app import adapters
from app.adapters import AdapterError, LTXAdapter, build_ltx_command, terminate_process_group, verify_video
from app.config import Settings


def _settings(tmp_path: Path) -> Settings:
    ltx_root = tmp_path / "ltx"
    ltx_root.mkdir(exist_ok=True)
    runner = ltx_root / "ltx-2-mlx"
    runner.write_text("test fixture; never executed")
    runner.chmod(0o755)
    hf_cache = tmp_path / "hf"
    hf_cache.mkdir(exist_ok=True)
    return Settings(_env_file=None, ltx_root=str(ltx_root), ltx_executable=str(runner),
                    hf_cache=str(hf_cache), allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "out"))


def _payload(tmp_path: Path, reference: str | None = None) -> dict:
    return {
        "job_id": "job-123", "prompt": "moonlit riverbank", "model": "dgrauet/ltx-2.3-mlx-q8",
        "width": 448, "height": 768, "frames": 49, "fps": 24, "seed": 7,
        "distilled_two_stage": True, "low_ram": True, "audio": False,
        "reference_image": reference, "output_location": str(tmp_path / "out"), "preset": "Short",
    }


def test_ltx_command_uses_installed_long_form_cli_and_image_binding(tmp_path):
    command = build_ltx_command("/local/ltx-2-mlx", _payload(tmp_path), "/out/job.partial.mp4", "/refs/riverbank.png")
    assert command == [
        "/local/ltx-2-mlx", "generate", "--prompt", "moonlit riverbank",
        "--model", "dgrauet/ltx-2.3-mlx-q8", "--width", "448", "--height", "768",
        "--frames", "49", "--frame-rate", "24", "--output", "/out/job.partial.mp4",
        "--image", "/refs/riverbank.png", "0", "1.0", "--seed", "7",
        "--distilled", "--low-ram", "--no-audio",
    ]


def test_ltx_text_to_video_omits_image_anchor(tmp_path):
    command = build_ltx_command("ltx", _payload(tmp_path), "/out/job.mp4")
    assert "--image" not in command
    assert command[command.index("--frames") + 1] == "49"


def test_adapter_invokes_exact_i2v_argv_offline_and_renames_verified_output(tmp_path, monkeypatch):
    async def run():
        settings = _settings(tmp_path)
        reference = tmp_path / "riverbank.png"
        reference.write_bytes(b"image fixture")
        payload = _payload(tmp_path, str(reference))
        captured = {}

        class Stream:
            lines = [b"generation started\n", b""]
            async def readline(self):
                return self.lines.pop(0)

        class Process:
            stdout = Stream()
            returncode = 0
            async def wait(self):
                return self.returncode

        async def fake_create(*args, **kwargs):
            captured["argv"] = list(args)
            captured["env"] = kwargs["env"]
            captured["new_session"] = kwargs["start_new_session"]
            output = Path(args[args.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"verified fixture")
            return Process()

        async def fake_verify(path, _settings, expected=None):
            assert path.endswith("job-123.partial.mp4")
            assert expected == {"width": 448, "height": 768, "fps": 24, "frames": 49}
            return {"verified_output": expected}

        monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fake_create)
        monkeypatch.setattr(adapters, "verify_video", fake_verify)
        result = await LTXAdapter(settings).generate(payload, lambda *_: asyncio.sleep(0))
        assert captured["argv"] == build_ltx_command(
            str(settings.expanded(settings.ltx_executable)), payload,
            str(tmp_path / "out" / "job-123.partial.mp4"), str(reference),
        )
        assert captured["argv"][captured["argv"].index("--image") + 1:][:3] == [str(reference), "0", "1.0"]
        assert captured["env"]["HF_HOME"] == str(settings.expanded(settings.hf_cache))
        assert captured["env"]["HF_HUB_OFFLINE"] == "1"
        assert captured["env"]["TRANSFORMERS_OFFLINE"] == "1"
        assert captured["new_session"] is True
        assert result["exit_code"] == 0
        assert Path(result["output_path"]).name == "job-123.mp4"
        assert Path(result["output_path"]).read_bytes() == b"verified fixture"
        assert not (tmp_path / "out" / "job-123.partial.mp4").exists()

    asyncio.run(run())


def test_adapter_cancellation_invokes_isolated_process_group_cleanup(tmp_path, monkeypatch):
    async def run():
        settings = _settings(tmp_path)
        payload = _payload(tmp_path)
        started = asyncio.Event()
        cleanup = []

        class BlockingStream:
            async def readline(self):
                await asyncio.Future()

        class FakeProcess:
            pid = 123456
            stdout = BlockingStream()
            returncode = None

        async def fake_create(*args, **kwargs):
            assert kwargs["start_new_session"] is True
            started.set()
            return FakeProcess()

        async def fake_cleanup(process):
            cleanup.append(process.pid)

        monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fake_create)
        monkeypatch.setattr(adapters, "terminate_process_group", fake_cleanup)
        task = asyncio.create_task(LTXAdapter(settings).generate(payload, lambda *_: asyncio.sleep(0)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup == [123456]

    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="Process group semantics are POSIX-specific")
def test_process_group_cleans_child_without_killing_unrelated_process(tmp_path):
    async def run():
        pid_file = tmp_path / "child.pid"
        parent_code = (
            "import pathlib, subprocess, sys, time; "
            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        parent = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent_code, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )
        unrelated = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE, start_new_session=True,
        )
        try:
            for _ in range(100):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.02)
            assert pid_file.exists()
            child_pid = int(pid_file.read_text())
            await terminate_process_group(parent, grace_seconds=0.5)
            assert parent.returncode is not None
            assert unrelated.returncode is None
            assert adapters._group_exists(unrelated.pid)
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("child process remained after its process group was stopped")
        finally:
            await terminate_process_group(unrelated, grace_seconds=0.5)

    asyncio.run(run())


def test_nonzero_exit_code_is_reported_and_partial_output_named(tmp_path, monkeypatch):
    async def run():
        settings = _settings(tmp_path)
        payload = _payload(tmp_path)
        class Stream:
            read = False
            async def readline(self):
                if not self.read:
                    self.read = True
                    return b"runner failed\n"
                return b""
        class FailedProcess:
            stdout = Stream()
            returncode = 23
            async def wait(self):
                return self.returncode
        async def fake_create(*args, **kwargs):
            output = Path(args[args.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"partial")
            return FailedProcess()
        monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fake_create)
        with pytest.raises(AdapterError) as error:
            await LTXAdapter(settings).generate(payload, lambda *_: asyncio.sleep(0))
        assert error.value.exit_code == 23
        assert error.value.partial_output.endswith("job-123.partial.mp4")

    asyncio.run(run())


def test_ffprobe_checks_resolution_rate_and_exact_frame_count(tmp_path, monkeypatch):
    async def run():
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fixture")
        settings = _settings(tmp_path)
        probe = {
            "format": {"duration": "2.041667"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 448, "height": 768,
                         "avg_frame_rate": "24/1", "nb_read_frames": "49"}],
        }
        class Probe:
            returncode = 0
            async def communicate(self):
                import json
                return json.dumps(probe).encode(), b""
        async def fake_create(*args, **kwargs):
            assert "-count_frames" in args
            return Probe()
        monkeypatch.setattr(adapters.shutil, "which", lambda _: "/usr/bin/ffprobe")
        monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fake_create)
        result = await verify_video(str(video), settings, {"width": 448, "height": 768, "fps": 24, "frames": 49})
        assert result["verified_output"] == {"width": 448, "height": 768, "fps": 24.0, "frames": 49}
        with pytest.raises(AdapterError, match="frames: expected 97, got 49"):
            await verify_video(str(video), settings, {"width": 448, "height": 768, "fps": 24, "frames": 97})

    asyncio.run(run())
