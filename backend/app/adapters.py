import asyncio
import os
import shutil
from pathlib import Path

from .config import Settings
from .paths import safe_local_path


class AdapterError(RuntimeError):
    pass


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
        output = safe_local_path(str(output_dir / (payload["job_id"] + ".mp4")), self.settings)
        args = [str(executable), "generate", "--prompt", payload["prompt"], "--model", payload["model"],
                "-W", str(payload["width"]), "-H", str(payload["height"]), "-f", str(payload["frames"]),
                "--frame-rate", str(payload["fps"]), "-o", str(output)]
        if payload.get("seed") is not None:
            args += ["--seed", str(payload["seed"])]
        args.append("--distilled" if payload.get("distilled_two_stage") else "--two-stage")
        if payload.get("low_ram"):
            args.append("--low-ram")
        if not payload.get("audio"):
            args.append("--no-audio")
        if payload.get("reference_image"):
            reference = safe_local_path(payload["reference_image"], self.settings, must_exist=True)
            args += ["--image", str(reference)]
        env = os.environ.copy()
        env["HF_HOME"] = str(self.settings.expanded(self.settings.hf_cache))
        hf_home = Path(env["HF_HOME"])
        env["HF_HUB_CACHE"] = str(hf_home / "hub") if (hf_home / "hub").is_dir() else str(hf_home)
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        await on_progress(5)
        process = await asyncio.create_subprocess_exec(*args, cwd=str(working_dir),
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
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
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise
        if code:
            raise AdapterError("LTX exited with code " + str(code) + ": " + "".join(output_text)[-3000:])
        await verify_video(str(output), self.settings)
        return str(output)


class RemotionAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def render(self, payload: dict, on_progress):
        if not self.settings.remotion_executable:
            raise AdapterError("Set REMOTION_EXECUTABLE to the local Remotion CLI before rendering")
        executable = Path(self.settings.remotion_executable).expanduser().resolve()
        safe_local_path(str(executable), self.settings, must_exist=True)
        working_dir = safe_local_path(self.settings.remotion_studio, self.settings, must_exist=True)
        entrypoint = safe_local_path(payload["entrypoint"], self.settings, must_exist=True)
        output = safe_local_path(payload["output_path"], self.settings)
        output.parent.mkdir(parents=True, exist_ok=True)
        args = [str(executable), "render", str(entrypoint), payload["composition_id"], str(output)]
        if payload.get("props"):
            import json
            args += ["--props", json.dumps(payload["props"], separators=(",", ":"))]
        await on_progress(2)
        process = await asyncio.create_subprocess_exec(*args, cwd=str(working_dir), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
        try:
            while line := await process.stdout.readline():
                await on_progress(35, line.decode(errors="replace").strip())
            code = await process.wait()
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise
        if code:
            raise AdapterError("Remotion exited with code " + str(code))
        await on_progress(90)
        await verify_video(str(output), self.settings)
        return str(output)


async def verify_video(path: str, settings: Settings) -> dict:
    video = safe_local_path(path, settings, must_exist=True)
    ffprobe = shutil.which(settings.ffprobe_executable)
    if not ffprobe:
        raise AdapterError("ffprobe executable was not found")
    proc = await asyncio.create_subprocess_exec(ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video),
             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode:
        raise AdapterError(stderr.decode(errors="replace")[:2000])
    import json
    return json.loads(stdout)
