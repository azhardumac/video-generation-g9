from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class ShotInput(BaseModel):
    id: str | None = None
    scene_title: str = Field(default="Scene 1", min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)
    reference_image: str | None = None
    preset: str = "Short"
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    caption: str = Field(default="", max_length=240)


class StoryboardUpdate(BaseModel):
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    shots: list[ShotInput] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def unique_shot_ids(self):
        ids = [shot.id for shot in self.shots if shot.id]
        if len(ids) != len(set(ids)):
            raise ValueError("Shot ids must be unique")
        return self


def build_composition_manifest(storyboard: dict, public_root: Path, transition_frames: int = 8) -> dict:
    """Build a deterministic portrait timeline from completed, verified shot outputs."""
    public_root = public_root.resolve()
    clips = []
    missing = []
    canvas = None
    for shot in sorted(storyboard["shots"], key=lambda item: (item["position"], item["id"])):
        job = shot.get("job")
        verified = (job.get("verification_metadata") or {}).get("verified_output") if job else None
        output = Path(job["result_path"]).expanduser().resolve() if job and job.get("result_path") else None
        if not job or job.get("status") != "completed" or not verified or output is None or not output.is_file():
            missing.append(shot["title"])
            continue
        size = (int(verified.get("width", 0)), int(verified.get("height", 0)))
        fps = int(verified.get("fps", 0))
        frames = int(verified.get("frames", 0))
        if size[0] <= 0 or size[1] <= size[0] or fps != 24 or frames <= 0:
            raise ValueError(f"Verified shot '{shot['title']}' must be portrait, 24 fps, and have a valid frame count")
        if canvas is None:
            canvas = (*size, fps)
        elif canvas != (*size, fps):
            raise ValueError("Verified shots must share the same portrait dimensions and 24 fps frame rate")
        try:
            relative = output.relative_to(public_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Shot '{shot['title']}' video must be inside OUTPUT_ROOT for Remotion public media") from exc
        clips.append({
            "shot_id": shot["id"], "scene_title": shot.get("scene_title", "Scene 1"),
            "title": shot["title"], "src": relative,
            "frames": frames, "fps": fps, "caption": shot.get("caption", ""),
            "prompt": shot["prompt"],
        })
    if missing:
        raise ValueError("Verified output is missing for: " + ", ".join(missing))
    if not clips:
        raise ValueError("Add at least one shot with a verified video output")

    cursor = 0
    previous_frames = 0
    for index, clip in enumerate(clips):
        overlap = min(transition_frames, previous_frames // 4, clip["frames"] // 4) if index else 0
        clip["transition_in_frames"] = overlap
        clip["start_frame"] = cursor - overlap
        cursor = clip["start_frame"] + clip["frames"]
        previous_frames = clip["frames"]
    for index, clip in enumerate(clips):
        clip["transition_out_frames"] = clips[index + 1]["transition_in_frames"] if index + 1 < len(clips) else 0
    width, height, fps = canvas
    return {
        "schema_version": 1,
        "storyboard_id": storyboard["id"],
        "revision": storyboard["revision"],
        "title": storyboard["title"],
        "width": width,
        "height": height,
        "fps": fps,
        "duration_in_frames": cursor,
        "transition": {"type": "crossfade", "default_frames": transition_frames},
        "shots": clips,
    }
