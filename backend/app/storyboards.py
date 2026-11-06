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
    width: int | None = Field(default=None, ge=64, le=2048)
    height: int | None = Field(default=None, ge=64, le=2048)
    fps: int | None = Field(default=None, ge=1, le=60)
    output_location: str | None = Field(default=None, max_length=4000)
    framing: str = Field(default="", max_length=120)
    prompt_reviewed: bool = False
    reviewed_style_revision: int = Field(default=0, ge=0)
    visual_description: str = Field(default="", max_length=4000)
    characters: list[str] = Field(default_factory=list, max_length=8)
    reference_ids: list[str] = Field(default_factory=list, max_length=8)
    estimated_seconds: float = Field(default=0, ge=0, le=3600)
    dialogue: str = Field(default="", max_length=1000)
    transition_after: str = Field(default="dissolve", max_length=32)
    generation_payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dimensions(self):
        if self.width is not None and self.width % 8:
            raise ValueError("Shot width must be divisible by 8")
        if self.height is not None and self.height % 8:
            raise ValueError("Shot height must be divisible by 8")
        return self


class StoryboardUpdate(BaseModel):
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    story_text: str = Field(default="", max_length=20000)
    shots: list[ShotInput] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def unique_shot_ids(self):
        ids = [shot.id for shot in self.shots if shot.id]
        if len(ids) != len(set(ids)):
            raise ValueError("Shot ids must be unique")
        return self


AUDIO_CUT_FADE_FRAMES = 2
AUDIO_LEVEL_MATCH = 0.5
AUDIO_MAX_GAIN_DB = 12.0
AUDIO_SILENCE_DB = -70.0
AUDIO_PEAK_CEILING_DB = -1.0


def build_composition_manifest(storyboard: dict, public_root: Path, transition_frames: int = 8) -> dict:
    """Build a deterministic portrait timeline from completed, verified shot outputs."""
    public_root = public_root.resolve()
    clips = []
    missing = []
    canvas = None
    for shot in sorted(storyboard["shots"], key=lambda item: (item["position"], item["id"])):
        job = shot.get("job")
        if job and (job.get("settings_match") is False or shot.get("settings_match") is False):
            raise ValueError(f"Shot '{shot['title']}' output is stale because generation settings changed")
        verified = (job.get("verification_metadata") or {}).get("verified_output") if job else None
        output = Path(job["result_path"]).expanduser().resolve() if job and job.get("result_path") else None
        if not job or job.get("status") != "completed" or not verified or output is None or not output.is_file():
            missing.append(shot["title"])
            continue
        size = (int(verified.get("width", 0)), int(verified.get("height", 0)))
        fps = int(verified.get("fps", 0))
        frames = int(verified.get("frames", 0))
        if size[0] <= 0 or size[1] <= 0 or fps <= 0 or frames <= 0:
            raise ValueError(f"Verified shot '{shot['title']}' must have positive dimensions, frame rate, and frame count")
        if canvas is None:
            canvas = (*size, fps)
        elif canvas != (*size, fps):
            raise ValueError("Verified shots must share the same dimensions and frame rate")
        try:
            relative = output.relative_to(public_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Shot '{shot['title']}' video must be inside OUTPUT_ROOT for Remotion public media") from exc
        clips.append({
            "shot_id": shot["id"], "scene_title": shot.get("scene_title", "Scene 1"),
            "title": shot["title"], "src": relative,
            "frames": frames, "fps": fps, "caption": shot.get("caption", ""),
            "prompt": shot["prompt"], "job_id": job.get("id"),
            "transition_after": shot.get("transition_after", "dissolve"),
            "has_audio": bool(job.get("payload", {}).get("audio")),
            "audio_levels": (job.get("verification_metadata") or {}).get("audio_levels") or {},
            "continues_previous": bool((job.get("verification_metadata") or {}).get("continued_from_previous_shot")),
            "settings_snapshot": job.get("settings_snapshot") or {
                "prompt": job.get("payload", {}).get("prompt", ""),
                "reference_image": job.get("payload", {}).get("reference_image") or None,
                "seed": job.get("payload", {}).get("seed"),
                "preset": job.get("payload", {}).get("preset", "Short"),
            },
        })
    if missing:
        raise ValueError("Verified output is missing for: " + ", ".join(missing))
    if not clips:
        raise ValueError("Add at least one shot with a verified video output")

    cursor = 0
    previous_frames = 0
    for index, clip in enumerate(clips):
        previous_transition = clips[index - 1].get("transition_after", "dissolve") if index else "cut"
        continues_previous = clip.pop("continues_previous")
        # A chained shot's first frame duplicates the previous clip's last frame; show it once at a hard cut.
        clip["trim_start_frames"] = 1 if (index and continues_previous and previous_transition in ("cut", "none")
                                          and not clips[index - 1]["trim_end_frames"]) else 0
        # LTX audio ends ~30 ms before its video. Dropping the last frame before a hard cut keeps each clip's
        # sound covering its whole slot, so cuts stay gapless without shifting sound off its picture.
        cut_out = index + 1 < len(clips) and clip.get("transition_after", "dissolve") in ("cut", "none")
        clip["trim_end_frames"] = 1 if clip["has_audio"] and cut_out else 0
        clip["frames"] -= clip["trim_start_frames"] + clip["trim_end_frames"]
        overlap = min(transition_frames, previous_frames // 4, clip["frames"] // 4) if index and previous_transition not in ("cut", "none") else 0
        clip["transition_in_frames"] = overlap
        clip["start_frame"] = cursor - overlap
        cursor = clip["start_frame"] + clip["frames"]
        previous_frames = clip["frames"]
    for index, clip in enumerate(clips):
        if index + 1 < len(clips):
            clip["transition_out_frames"] = clips[index + 1]["transition_in_frames"]
        elif clip.get("transition_after") == "fade":
            clip["transition_out_frames"] = min(transition_frames, clip["frames"] // 4)
        else:
            clip["transition_out_frames"] = 0
        cut_in = bool(index) and not clip["transition_in_frames"]
        cut_out = index + 1 < len(clips) and not clip["transition_out_frames"]
        cut_fade = min(AUDIO_CUT_FADE_FRAMES, clip["frames"] // 4)
        clip["audio_fade_in_frames"] = cut_fade if cut_in else clip["transition_in_frames"]
        clip["audio_fade_out_frames"] = cut_fade if cut_out else clip["transition_out_frames"]
    # Bring each shot's sound halfway to the film's median level: background stays even across cuts,
    # while deliberately loud or quiet moments keep some of their contrast.
    measured = {}
    for clip in clips:
        levels = clip.pop("audio_levels")
        if clip["has_audio"] and levels.get("mean_db") is not None and levels["mean_db"] >= AUDIO_SILENCE_DB:
            measured[clip["shot_id"]] = levels
    means = sorted(levels["mean_db"] for levels in measured.values())
    median = (means[(len(means) - 1) // 2] + means[len(means) // 2]) / 2 if means else None
    for clip in clips:
        levels = measured.get(clip["shot_id"])
        gain = 0.0 if median is None or levels is None else (median - levels["mean_db"]) * AUDIO_LEVEL_MATCH
        # Never boost a clip's loudest moment above AUDIO_PEAK_CEILING_DB, so a quiet shot with a splash doesn't clip.
        if levels and levels.get("peak_db") is not None:
            gain = min(gain, AUDIO_PEAK_CEILING_DB - levels["peak_db"])
        clip["audio_gain_db"] = round(max(-AUDIO_MAX_GAIN_DB, min(AUDIO_MAX_GAIN_DB, gain)), 1)
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
