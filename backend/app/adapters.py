import asyncio
import json
import os
import shutil
import signal
from fractions import Fraction
from pathlib import Path

from .config import Settings
from .paths import safe_local_path


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int | None = None, partial_output: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.partial_output = partial_output


def build_ltx_command(executable: str, payload: dict, output_path: str, reference_image: str | None = None) -> list[str]:
    """Build argv for the installed ltx-2-mlx generate CLI without shell parsing."""
    args = [
        executable, "generate", "--prompt", payload["prompt"], "--model", payload["model"],
        "--width", str(payload["width"]), "--height", str(payload["height"]),
        "--frames", str(payload["frames"]), "--frame-rate", str(payload["fps"]),
        "--output", output_path,
    ]
    if reference_image:
        args += ["--image", reference_image, "0", "1.0"]
    if payload.get("seed") is not None:
        args += ["--seed", str(payload["seed"])]
    args.append("--distilled" if payload.get("distilled_two_stage") else "--two-stage")
    if payload.get("low_ram"):
        args.append("--low-ram")
    if not payload.get("audio"):
        args.append("--no-audio")
    return args


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def terminate_process_group(process: asyncio.subprocess.Process, grace_seconds: float = 5.0) -> None:
    """Stop only the process group created for this adapter invocation."""
    if os.name != "posix":
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), grace_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        return

    pgid = process.pid  # start_new_session=True makes the child PID the group ID.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), grace_seconds)
        except asyncio.TimeoutError:
            pass

    # The CLI can exit before grandchildren. Keep signaling only this isolated group.
    deadline = asyncio.get_running_loop().time() + grace_seconds
    while _group_exists(pgid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


class LTXAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def generate(self, payload: dict, on_progress):
        if not self.settings.ltx_executable:
            raise AdapterError("Set LTX_EXECUTABLE to your local LTX-2.3 MLX runner before generating")
        executable = Path(self.settings.ltx_executable).expanduser().resolve()
        safe_local_path(str(executable), self.settings, must_exist=True)
        working_dir = safe_local_path(self.settings.ltx_root, self.settings, must_exist=True)
        output_dir = safe_local_path(payload["output_location"], self.settings)
        output_dir.mkdir(parents=True, exist_ok=True)
        job_id = payload["job_id"]
        output = safe_local_path(str(output_dir / (job_id + ".mp4")), self.settings)
        partial = safe_local_path(str(output_dir / (job_id + ".partial.mp4")), self.settings)
        reference = None
        if payload.get("reference_image"):
            reference = safe_local_path(payload["reference_image"], self.settings, must_exist=True)
        args = build_ltx_command(str(executable), payload, str(partial), str(reference) if reference else None)

        env = os.environ.copy()
        env["HF_HOME"] = str(self.settings.expanded(self.settings.hf_cache))
        hf_home = Path(env["HF_HOME"])
        env["HF_HUB_CACHE"] = str(hf_home / "hub") if (hf_home / "hub").is_dir() else str(hf_home)
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        await on_progress(5)
        process = await asyncio.create_subprocess_exec(
            *args, cwd=str(working_dir), env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=(os.name == "posix"),
        )
        output_text = []
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").strip()
                output_text.append(decoded[:1000])
                await on_progress(min(90, 10 + len(output_text)), decoded)
            code = await process.wait()
        except asyncio.CancelledError:
            await terminate_process_group(process)
            if partial.exists():
                await on_progress(0, f"Partial output retained: {partial}")
            raise
        if code:
            raise AdapterError(
                f"LTX exited with code {code}: {''.join(output_text)[-3000:]}",
                exit_code=code,
                partial_output=str(partial) if partial.exists() else None,
            )
        try:
            metadata = await verify_video(
                str(partial), self.settings,
                expected={"width": payload["width"], "height": payload["height"],
                          "fps": payload["fps"], "frames": payload["frames"]},
            )
        except AdapterError as exc:
            if partial.exists():
                exc.partial_output = str(partial)
            raise
        os.replace(partial, output)
        await on_progress(95, "Verified expected dimensions, frame rate, and frame count")
        return {"output_path": str(output), "metadata": metadata, "exit_code": 0}


class RemotionAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def render(self, payload: dict, on_progress):
        if not self.settings.remotion_executable:
            raise AdapterError("Set REMOTION_EXECUTABLE to the local Remotion CLI before rendering")
        executable = Path(self.settings.remotion_executable).expanduser().resolve()
        safe_local_path(str(executable), self.settings, must_exist=True)
        working_dir = safe_local_path(self.settings.remotion_studio, self.settings, must_exist=True)
        requested_entrypoint = Path(payload["entrypoint"]).expanduser().resolve(strict=True)
        packaged_entrypoint = Path(__file__).resolve().parents[2] / "remotion" / "index.ts"
        entrypoint = requested_entrypoint if requested_entrypoint == packaged_entrypoint else safe_local_path(
            str(requested_entrypoint), self.settings, must_exist=True)
        output = safe_local_path(payload["output_path"], self.settings)
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.stem + ".partial" + output.suffix)
        args = [str(executable), "render", str(entrypoint), payload["composition_id"], str(partial), "--overwrite"]
        if payload.get("width"):
            args += ["--width", str(payload["width"])]
        if payload.get("height"):
            args += ["--height", str(payload["height"])]
        if payload.get("fps"):
            args += ["--fps", str(payload["fps"])]
        if payload.get("public_dir"):
            public_dir = safe_local_path(payload["public_dir"], self.settings, must_exist=True)
            args += ["--public-dir", str(public_dir)]
        if payload.get("props"):
            args += ["--props", json.dumps(payload["props"], separators=(",", ":"))]
        await on_progress(2)
        env = os.environ.copy()
        remotion_modules = str(working_dir / "node_modules")
        env["NODE_PATH"] = os.pathsep.join(filter(None, (remotion_modules, env.get("NODE_PATH", ""))))
        process = await asyncio.create_subprocess_exec(
            *args, cwd=str(working_dir), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=(os.name == "posix"), env=env,
        )
        try:
            while line := await process.stdout.readline():
                await on_progress(35, line.decode(errors="replace").strip())
            code = await process.wait()
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        if code:
            raise AdapterError(
                f"Remotion exited with code {code}", exit_code=code,
                partial_output=str(partial) if partial.exists() else None,
            )
        await on_progress(90)
        metadata = await verify_video(str(partial), self.settings, expected=payload.get("expected_output"))
        os.replace(partial, output)
        return {"output_path": str(output), "metadata": metadata, "exit_code": 0}


async def verify_video(path: str, settings: Settings, expected: dict | None = None) -> dict:
    video = safe_local_path(path, settings, must_exist=True)
    ffprobe = shutil.which(settings.ffprobe_executable)
    if not ffprobe:
        raise AdapterError("ffprobe executable was not found")
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_format", "-show_streams", "-of", "json", str(video),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode:
        raise AdapterError(stderr.decode(errors="replace")[:2000], exit_code=proc.returncode)
    result = json.loads(stdout)
    streams = result.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video_stream:
        raise AdapterError("ffprobe found no video stream")
    metadata = result | {"video": video_stream}
    if expected:
        actual = {
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "fps": _parse_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
            "frames": int(video_stream.get("nb_read_frames") or video_stream.get("nb_frames") or 0),
        }
        mismatches = [f"{key}: expected {value}, got {actual[key]}" for key, value in expected.items()
                      if actual.get(key) != value]
        if mismatches:
            raise AdapterError("Output verification failed: " + "; ".join(mismatches))
        metadata["verified_output"] = actual
    return metadata


def _parse_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
