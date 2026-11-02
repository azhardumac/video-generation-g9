import asyncio
from pathlib import Path

from app import worker as worker_module
from app.adapters import build_ltx_command
from app.config import Settings
from app.storyboards import build_composition_manifest
from app.worker import LocalWorker


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "out"))


def test_ltx_command_passes_anchor_binding():
    payload = {"prompt": "river", "model": "m", "width": 448, "height": 768, "frames": 49, "fps": 24,
               "reference_anchors": [{"path": "/out/prev.png", "frame_index": 0, "strength": 1.0, "binding": "replace"},
                                     {"path": "/refs/frog.png", "frame_index": 0, "strength": 0.35, "binding": "guide"}]}
    command = build_ltx_command("ltx", payload, "/out/job.partial.mp4")
    assert command[command.index("--image"):command.index("--image") + 10] == [
        "--image", "/out/prev.png", "0", "1.0", "replace", "--image", "/refs/frog.png", "0", "0.35", "guide"]


def test_film_reference_kept_for_snapshots_is_not_pinned_as_first_frame():
    payload = {"prompt": "river", "model": "m", "width": 448, "height": 768, "frames": 49, "fps": 24,
               "reference_image": "/refs/frog.png", "reference_as_frame": False, "reference_anchors": []}
    assert "--image" not in build_ltx_command("ltx", payload, "/out/job.partial.mp4", "/refs/frog.png")


class _Db:
    def __init__(self, clip):
        self.clip, self.events = clip, []

    def film_batch_shot_output(self, batch_id, shot_id):
        return self.clip if (batch_id, shot_id) == ("batch", "previous") else None

    def event(self, job_id, level, message):
        self.events.append((level, message))


def _worker(tmp_path, clip):
    worker = LocalWorker.__new__(LocalWorker)
    worker.db, worker.settings = _Db(clip), _settings(tmp_path)
    return worker


def _job(tmp_path):
    (tmp_path / "out").mkdir(exist_ok=True)
    return {"id": "job-2", "payload": {"film_batch_id": "batch", "continue_from_shot_id": "previous",
            "output_location": str(tmp_path / "out"), "reference_image": None,
            "reference_anchors": [{"path": "/refs/frog.png", "frame_index": 0, "strength": 0.35, "binding": "guide"}]}}


def test_chained_shot_starts_on_previous_clip_last_frame(tmp_path, monkeypatch):
    clip = tmp_path / "out" / "job-1.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"clip")

    async def fake_extract(video, image, settings):
        assert video == str(clip)
        image.write_bytes(b"png")
        return image

    monkeypatch.setattr(worker_module, "extract_last_frame", fake_extract)
    payload = asyncio.run(_worker(tmp_path, str(clip))._with_previous_shot(_job(tmp_path)))
    assert payload["continued_from_previous_shot"] is True
    assert payload["reference_anchors"] == [
        {"path": str((tmp_path / "out" / "job-2.continue.png").resolve()), "frame_index": 0, "strength": 1.0, "binding": "replace"}]


def test_chained_shot_falls_back_when_previous_clip_is_missing(tmp_path):
    worker = _worker(tmp_path, None)
    job = _job(tmp_path)
    assert asyncio.run(worker._with_previous_shot(job)) is job["payload"]
    assert worker.db.events[0][0] == "warning"


def _shot(tmp_path, position, continues):
    output = tmp_path / f"shot{position}.mp4"
    output.write_bytes(b"clip")
    return {"id": f"s{position}", "position": position, "title": f"Shot {position}", "prompt": "p", "transition_after": "cut",
            "job": {"id": f"j{position}", "status": "completed", "result_path": str(output),
                    "verification_metadata": {"verified_output": {"width": 448, "height": 768, "fps": 24, "frames": 49},
                                              "continued_from_previous_shot": continues}}}


def test_manifest_drops_duplicated_first_frame_of_chained_shots(tmp_path):
    storyboard = {"id": "sb", "revision": 1, "title": "Film",
                  "shots": [_shot(tmp_path, 0, False), _shot(tmp_path, 1, True), _shot(tmp_path, 2, False)]}
    manifest = build_composition_manifest(storyboard, tmp_path)
    assert [(shot["trim_start_frames"], shot["frames"], shot["start_frame"]) for shot in manifest["shots"]] == [
        (0, 49, 0), (1, 48, 49), (0, 49, 97)]
