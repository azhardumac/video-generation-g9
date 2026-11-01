"""Strict, project-scoped story-plan contract and optional local planner CLI.

The adapter never downloads a model or falls back to keyword heuristics. A local
planner must be explicitly configured as a JSON argument array and implement the
stdin/stdout contract below. Its output is treated as untrusted and validated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class PlannerUnavailable(RuntimeError):
    """A general-purpose local story planner is not configured or runnable."""


class PlannerOutputError(ValueError):
    """The planner returned malformed or out-of-scope story data."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StoryCharacter(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    identity: str = Field(min_length=12, max_length=1200)
    distinguishing_traits: list[str] = Field(min_length=1, max_length=12)
    reference_ids: list[str] = Field(default_factory=list, max_length=16)


class StoryLocation(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=12, max_length=1200)
    scale: str = Field(min_length=3, max_length=300)
    continuity_requirements: list[str] = Field(min_length=1, max_length=12)


class StoryShot(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    sequence: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=160)
    beat: str = Field(min_length=8, max_length=1000)
    prompt: str = Field(min_length=20, max_length=4000)
    framing: str = Field(min_length=3, max_length=240)
    character_layout: str = Field(min_length=5, max_length=500)
    characters_present: list[str] = Field(min_length=1, max_length=20)
    expected_character_count: int = Field(ge=1, le=20)
    reference_ids: list[str] = Field(default_factory=list, max_length=1)
    caption: str = Field(default="", max_length=1000)
    transition_after: Literal["cut", "dissolve", "fade"] = "cut"


class StoryScene(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    sequence: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=160)
    story_beat: str = Field(min_length=8, max_length=1000)
    location_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    characters_present: list[str] = Field(min_length=1, max_length=20)
    visual_description: str = Field(min_length=20, max_length=2000)
    framing: str = Field(min_length=3, max_length=240)
    character_layout: str = Field(min_length=5, max_length=800)
    continuity_requirements: list[str] = Field(min_length=1, max_length=16)
    expected_character_count: int = Field(ge=1, le=20)
    shots: list[StoryShot] = Field(min_length=1, max_length=40)


class StoryPlan(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    logline: str = Field(min_length=12, max_length=1000)
    visual_style: str = Field(min_length=12, max_length=1200)
    audio_requested: bool
    video_continuation_requested: bool
    ic_lora_requested: bool
    characters: list[StoryCharacter] = Field(min_length=1, max_length=20)
    locations: list[StoryLocation] = Field(min_length=1, max_length=20)
    scenes: list[StoryScene] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_references_and_continuity(self):
        def unique(items, label):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} identifiers must be unique")

        unique(self.characters, "Character")
        unique(self.locations, "Location")
        unique(self.scenes, "Scene")
        if any(len(item.reference_ids) != len(set(item.reference_ids)) for item in self.characters):
            raise ValueError("A character cannot repeat a reference identifier")
        if [scene.sequence for scene in self.scenes] != list(range(len(self.scenes))):
            raise ValueError("Scene sequence numbers must preserve the listed story order")
        character_ids = {item.id for item in self.characters}
        location_ids = {item.id for item in self.locations}
        shot_ids = [shot.id for scene in self.scenes for shot in scene.shots]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("Shot identifiers must be unique across the film")
        for scene in self.scenes:
            if [shot.sequence for shot in scene.shots] != list(range(len(scene.shots))):
                raise ValueError(f"Shot sequence numbers in scene '{scene.title}' must preserve their listed order")
            if scene.location_id not in location_ids:
                raise ValueError(f"Scene '{scene.title}' refers to an unknown location")
            if not set(scene.characters_present).issubset(character_ids):
                raise ValueError(f"Scene '{scene.title}' refers to an unknown character")
            if scene.expected_character_count != len(set(scene.characters_present)):
                raise ValueError(f"Scene '{scene.title}' expected character count does not match its character roster")
            if len(scene.characters_present) != len(set(scene.characters_present)):
                raise ValueError(f"Scene '{scene.title}' repeats a character identity")
            for shot in scene.shots:
                if len(shot.reference_ids) != len(set(shot.reference_ids)):
                    raise ValueError(f"Shot '{shot.title}' repeats an image reference")
                if not set(shot.characters_present).issubset(character_ids):
                    raise ValueError(f"Shot '{shot.title}' refers to an unknown character")
                if not set(shot.characters_present).issubset(scene.characters_present):
                    raise ValueError(f"Shot '{shot.title}' changes the scene character layout")
                if shot.expected_character_count != len(set(shot.characters_present)):
                    raise ValueError(f"Shot '{shot.title}' expected character count does not match its character roster")
                if len(shot.characters_present) != len(set(shot.characters_present)):
                    raise ValueError(f"Shot '{shot.title}' repeats a character identity")
        return self

    @property
    def ordered_shots(self) -> list[tuple[StoryScene, StoryShot]]:
        return [(scene, shot) for scene in self.scenes for shot in scene.shots]

    def validate_project_scope(self, allowed_reference_ids: set[str], reference_categories: dict[str, str] | None = None) -> "StoryPlan":
        for character in self.characters:
            if not set(character.reference_ids).issubset(allowed_reference_ids):
                raise PlannerOutputError(f"Character '{character.name}' selected a reference that is not linked to this project")
            if reference_categories and any(reference_categories.get(reference_id) != "character" for reference_id in character.reference_ids):
                raise PlannerOutputError(f"Character '{character.name}' selected a non-character image reference")
        for scene in self.scenes:
            for shot in scene.shots:
                if not set(shot.reference_ids).issubset(allowed_reference_ids):
                    raise PlannerOutputError(f"Shot '{shot.title}' selected a reference that is not linked to this project")
                allowed_for_scene = {reference_id for character in self.characters
                                     if character.id in shot.characters_present
                                     for reference_id in character.reference_ids}
                if not set(shot.reference_ids).issubset(allowed_for_scene):
                    raise PlannerOutputError(f"Shot '{shot.title}' uses a reference for a character who is not present")
        return self


@dataclass
class PlannerResult:
    """Validated plan plus safe provider provenance, shared across adapters."""

    plan: StoryPlan
    provenance: dict[str, Any]


class StoryPlanner(Protocol):
    """Provider-neutral contract consumed by the durable FilmRunEngine."""

    provider: str

    def plan(self, request: dict) -> StoryPlan | PlannerResult:
        """Return a strict StoryPlan, optionally wrapped with provenance."""
        ...


class LocalStoryPlanner:
    """Call a user-configured local executable using JSON over stdin/stdout."""

    provider = "local"

    def __init__(self, command_json: str = "", timeout_seconds: int = 300):
        self.command_json = command_json.strip()
        self.timeout_seconds = timeout_seconds

    def command(self) -> list[str] | None:
        if not self.command_json:
            return None
        try:
            command = json.loads(self.command_json)
        except json.JSONDecodeError as exc:
            raise PlannerUnavailable("STORY_PLANNER_COMMAND must be a JSON array of executable and arguments") from exc
        if (not isinstance(command, list) or not command or
                any(not isinstance(part, str) or not part or "\x00" in part for part in command)):
            raise PlannerUnavailable("STORY_PLANNER_COMMAND must be a non-empty JSON array of strings")
        executable = command[0]
        resolved = shutil.which(executable)
        if not resolved:
            return None
        return [resolved, *command[1:]]

    def availability(self) -> dict[str, str | bool]:
        if not self.command_json:
            return {"available": False, "status": "not_configured",
                    "detail": "No local general story-planning model is configured. No plan or generation job will be created."}
        try:
            command = self.command()
        except PlannerUnavailable as exc:
            return {"available": False, "status": "invalid_configuration", "detail": str(exc)}
        if not command:
            return {"available": False, "status": "executable_missing",
                    "detail": "The configured story planner executable is unavailable on PATH; this run is blocked until it is installed and configured locally."}
        return {"available": True, "status": "configured_unverified",
                "detail": "A local planner command is configured. Its output is validated on each run; model behavior is not pre-certified."}

    def plan(self, request: dict) -> StoryPlan:
        command = self.command()
        if not command:
            raise PlannerUnavailable("No local general story-planning model is configured. Configure STORY_PLANNER_COMMAND; the app will not invent a plan or call a cloud service.")
        contract = {
            "contract": "FRAMEWORK story plan v1",
            "requirements": [
                "Return only one JSON object matching the provided JSON Schema.",
                "Use only reference ids in the provided project_reference_catalog; never invent or infer a file path.",
                "Preserve story character identities, ordered causal beats, locations, framing and continuity requirements.",
                "Do not add characters or plot events absent from the prompt unless needed to make an explicit transition coherent.",
                "Create a shot count within the supplied shot_budget and choose at most one image reference per shot.",
                "LTX is independently image-conditioned per shot; references do not guarantee stable identity across shots.",
                "Use the supplied model_capabilities as authoritative. Do not plan audio, video continuation, IC-LoRA, or multi-image identity conditioning when unavailable.",
            ],
            "input": request,
            "json_schema": StoryPlan.model_json_schema(),
        }
        try:
            env = os.environ.copy()
            env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1"})
            result = subprocess.run(command, input=json.dumps(contract, ensure_ascii=False), text=True,
                                    capture_output=True, check=False, timeout=self.timeout_seconds,
                                    shell=False, close_fds=True, env=env)
        except subprocess.TimeoutExpired as exc:
            raise PlannerUnavailable("The configured local story planner exceeded its time limit; no shot jobs were queued.") from exc
        except OSError as exc:
            raise PlannerUnavailable(f"Could not start the configured local story planner: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "no diagnostic output").strip()[-1600:]
            raise PlannerUnavailable(f"The local story planner exited with code {result.returncode}: {detail}")
        if len(result.stdout.encode("utf-8")) > 8 * 1024 * 1024:
            raise PlannerOutputError("Local story planner output exceeded the 8 MiB response limit")
        try:
            raw = json.loads(result.stdout)
            plan = StoryPlan.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise PlannerOutputError(f"Local story planner returned invalid structured data: {exc}") from exc
        categories = {item["id"]: item["category"] for item in request.get("project_reference_catalog", [])}
        return plan.validate_project_scope(set(request.get("allowed_reference_ids", [])), categories)
