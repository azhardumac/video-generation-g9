import asyncio
import json
import os
import re
import shutil
import socket
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


def select_remotion_render_port(configured_port: int = 0) -> int:
    """Return a free loopback port; zero means ask the OS for an ephemeral port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if configured_port:
            try:
                probe.bind(("127.0.0.1", configured_port))
            except OSError as exc:
                raise AdapterError(
                    f"Configured Remotion render port {configured_port} is unavailable: {exc}. "
                    "Choose another REMOTION_RENDER_PORT or set it to 0 for a dynamic port."
                ) from exc
            return configured_port
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_ltx_command(executable: str, payload: dict, output_path: str, reference_image: str | None = None) -> list[str]:
    """Build argv for the installed ltx-2-mlx generate CLI without shell parsing."""
    args = [
        executable, "generate", "--prompt", payload["prompt"], "--model", payload["model"],
        "--width", str(payload["width"]), "--height", str(payload["height"]),
        "--frames", str(payload.get("generate_frames") or payload["frames"]), "--frame-rate", str(payload["fps"]),
        "--output", output_path,
    ]
    anchors = payload.get("reference_anchors") or []
    if anchors:
        for anchor in anchors:
            args += ["--image", anchor["path"], str(anchor.get("frame_index", 0)), str(anchor.get("strength", 1.0)),
                     *([anchor["binding"]] if anchor.get("binding") else [])]
    elif reference_image and payload.get("reference_as_frame", True):
        args += ["--image", reference_image, "0", "1.0"]
    if payload.get("seed") is not None:
        args += ["--seed", str(payload["seed"])]
    args.append("--distilled" if payload.get("distilled_two_stage") else "--two-stage")
    if payload.get("negative_prompt"):
        args += ["--negative-prompt", payload["negative_prompt"]]
        # The distilled path has no CFG pass; NAG applies the negative prompt inside its cross-attention instead.
        if payload.get("distilled_two_stage"):
            args.append("--nag")
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


# Progress bars redraw with carriage returns, so one "line" can be very long.
SUBPROCESS_LINE_LIMIT = 16 * 1024 * 1024


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


def _ffmpeg_executable(settings: Settings) -> str | None:
    ffprobe = shutil.which(settings.ffprobe_executable)
    sibling = Path(ffprobe).with_name("ffmpeg") if ffprobe else None
    return str(sibling) if sibling and sibling.is_file() else shutil.which("ffmpeg")


async def measure_audio_levels(video: str, settings: Settings) -> dict[str, float] | None:
    """Mean and peak audio level in dBFS, used to even out background sound between stitched shots."""
    ffmpeg = _ffmpeg_executable(settings)
    if not ffmpeg:
        return None
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-nostats", "-i", video, "-vn", "-af", "volumedetect", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    text = stderr.decode(errors="replace")
    mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", text)
    peak = re.search(r"max_volume:\s*(-?[0-9.]+) dB", text)
    if process.returncode or not mean or not peak:
        return None
    return {"mean_db": float(mean.group(1)), "peak_db": float(peak.group(1))}


async def trim_to_frames(video: Path, frames: int, fps: int, settings: Settings) -> None:
    """Keep only the first `frames` frames (and matching audio) of a clip, in place."""
    ffmpeg = _ffmpeg_executable(settings)
    if not ffmpeg:
        raise AdapterError("ffmpeg executable was not found")
    trimmed = video.with_name(video.stem + ".trim.mp4")
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-v", "error", "-y", "-i", str(video), "-frames:v", str(frames), "-t", f"{frames / fps:.6f}",
        "-c:v", "libx264", "-crf", "14", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(trimmed),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        trimmed.unlink(missing_ok=True)
        raise
    if process.returncode or not trimmed.is_file():
        trimmed.unlink(missing_ok=True)
        raise AdapterError("Could not trim the clip to its planned length: " + stderr.decode(errors="replace")[-500:])
    os.replace(trimmed, video)


async def extract_last_frame(video: str, image: Path, settings: Settings) -> Path:
    """Write the final frame of a verified clip as a still for the next shot's first-frame anchor."""
    ffmpeg = _ffmpeg_executable(settings)
    if not ffmpeg:
        raise AdapterError("ffmpeg executable was not found")
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-v", "error", "-y", "-sseof", "-0.5", "-i", video, "-update", "1", "-frames:v", "1000", str(image),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    if process.returncode or not image.is_file():
        raise AdapterError("Could not extract the previous shot's last frame: " + stderr.decode(errors="replace")[-500:])
    return image


async def extract_first_frame(video: str, image: Path, settings: Settings) -> Path:
    """Write the first frame of a clip as a still."""
    ffmpeg = _ffmpeg_executable(settings)
    if not ffmpeg:
        raise AdapterError("ffmpeg executable was not found")
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-v", "error", "-y", "-i", video, "-frames:v", "1", str(image),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    if process.returncode or not image.is_file():
        raise AdapterError("Could not extract the plate's first frame: " + stderr.decode(errors="replace")[-500:])
    return image


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
        # A chained shot already opens on the previous clip's last frame.
        if payload.get("keyframe") and not payload.get("continued_from_previous_shot"):
            opening = await self._opening_frame(payload, output_dir, job_id, on_progress)
            if opening:
                payload = payload | {"generate_frames": payload["frames"],
                                     "reference_anchors": [{"path": str(opening), "frame_index": 0, "strength": 1.0}]}
        reference = None
        if payload.get("reference_image"):
            reference = safe_local_path(payload["reference_image"], self.settings, must_exist=True)
        anchors = []
        for anchor in payload.get("reference_anchors", []):
            anchor_path = safe_local_path(anchor["path"], self.settings, must_exist=True)
            anchors.append(anchor | {"path": str(anchor_path)})
        args = build_ltx_command(str(executable), payload | {"reference_anchors": anchors}, str(partial), str(reference) if reference else None)

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
            limit=SUBPROCESS_LINE_LIMIT,
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
        except BaseException:
            if process.returncode is None:
                await terminate_process_group(process)
            raise
        if code:
            raise AdapterError(
                f"LTX exited with code {code}: {''.join(output_text)[-3000:]}",
                exit_code=code,
                partial_output=str(partial) if partial.exists() else None,
            )
        try:
            if (payload.get("generate_frames") or payload["frames"]) > payload["frames"]:
                await on_progress(92, "Trimming the reference-guided tail off the clip")
                await trim_to_frames(partial, payload["frames"], payload["fps"], self.settings)
            metadata = await verify_video(
                str(partial), self.settings,
                expected={"width": payload["width"], "height": payload["height"],
                          "fps": payload["fps"], "frames": payload["frames"]},
            )
        except AdapterError as exc:
            if partial.exists():
                exc.partial_output = str(partial)
            raise
        if payload.get("audio"):
            metadata["audio_levels"] = await measure_audio_levels(str(partial), self.settings)
        os.replace(partial, output)
        await on_progress(95, "Verified expected dimensions, frame rate, and frame count")
        return {"output_path": str(output), "metadata": metadata, "exit_code": 0}

    async def _opening_frame(self, payload: dict, output_dir: Path, job_id: str, on_progress) -> Path | None:
        """The shot's first frame: a short LTX plate of the empty location with the character sheets pasted on."""
        keyframe = payload["keyframe"]

        async def plate_progress(_value, message=None):
            await on_progress(2, *([f"Opening frame: {message}"] if message else []))

        plate_payload = {key: value for key, value in payload.items() if key != "keyframe"} | {
            "job_id": job_id + ".plate", "prompt": keyframe["plate_prompt"], "frames": 9, "generate_frames": 9,
            "audio": False, "reference_anchors": [], "reference_image": None, "distilled_two_stage": True}
        clip = None
        try:
            clip = Path((await self.generate(plate_payload, plate_progress))["output_path"])
            plate = await extract_first_frame(str(clip), safe_local_path(str(output_dir / (job_id + ".plate.png")), self.settings), self.settings)
            sheets = [item | {"path": str(safe_local_path(item["path"], self.settings, must_exist=True))}
                      for item in keyframe["characters"]]
            opening = safe_local_path(str(output_dir / (job_id + ".opening.png")), self.settings)
            from .keyframes import compose_opening_frame
            if not await asyncio.to_thread(compose_opening_frame, str(plate), sheets, str(opening)):
                raise ValueError("a character sheet has no plain light background to cut it from")
        except (AdapterError, OSError, ValueError, ImportError) as exc:
            await on_progress(3, f"Opening frame unavailable ({exc}); generating from the prompt alone")
            return None
        finally:
            if clip:
                clip.unlink(missing_ok=True)
        await on_progress(4, "Opening frame built from the approved character sheets: " + str(opening))
        return opening


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
        render_port = select_remotion_render_port(self.settings.remotion_render_port)
        args = [str(executable), "render", str(entrypoint), payload["composition_id"], str(partial), "--overwrite"]
        args += ["--port", str(render_port)]
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
        try:
            process = await asyncio.create_subprocess_exec(
                *args, cwd=str(working_dir), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=(os.name == "posix"), env=env,
                limit=SUBPROCESS_LINE_LIMIT,
            )
        except OSError as exc:
            raise AdapterError(f"Could not start Remotion render on port {render_port}: {exc}") from exc
        output_text = []
        try:
            while line := await process.stdout.readline():
                decoded = line.decode(errors="replace").strip()
                if decoded:
                    output_text.append(decoded[:1000])
                    if len(output_text) > 100:
                        del output_text[0]
                await on_progress(35, decoded)
            code = await process.wait()
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        except BaseException:
            if process.returncode is None:
                await terminate_process_group(process)
            raise
        if code:
            details = "\n".join(output_text)[-8000:]
            suffix = f"\nFinal Remotion output:\n{details}" if details else "\nRemotion produced no stdout/stderr output."
            raise AdapterError(
                f"Remotion exited with code {code} on renderer port {render_port}.{suffix}", exit_code=code,
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
