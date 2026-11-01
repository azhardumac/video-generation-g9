"""Connect authorized prompt runs to a local planner and the existing job queue."""

from __future__ import annotations

from pathlib import Path
import shutil
from statistics import median
from uuid import UUID, uuid5, NAMESPACE_URL

from .config import Settings
from .film_orchestrator import FilmOrchestrator, FilmRunTransitionError, enforce_generation_budget
from .integrations import detect_integrations
from .paths import safe_local_path
from .presets import BUILTIN_PRESETS, GenerationSettings
from .story_planning import LocalStoryPlanner, PlannerOutputError, PlannerUnavailable, StoryPlan


class FilmRunEngine:
    """Advance one durable stage per call; LTX jobs remain on LocalWorker's queue."""

    def __init__(self, db, settings: Settings, planner: LocalStoryPlanner | None = None,
                 ltx_ready=None):
        self.db = db
        self.settings = settings
        self.orchestrator = FilmOrchestrator(db)
        self.planner = planner or LocalStoryPlanner(settings.story_planner_command,
                                                    settings.story_planner_timeout_seconds)
        self.ltx_ready = ltx_ready or (lambda: detect_integrations(settings)["ltx"]["ready"])

    def process_next(self):
        run = self.db.next_film_run()
        if not run:
            return None
        if run["stage"] == "inspect_clips":
            batch = self._batch_for(run)
            if batch and batch["status"] == "running":
                return None
        claimed = self.db.update_film_run(run["id"], run["revision"], status="running",
                                          message=f"Started prompt-first stage: {run['stage']}.")
        try:
            if run["stage"] == "understand_story":
                return self._understand(claimed)
            if run["stage"] == "define_visual_bible":
                return self._define_style(claimed)
            if run["stage"] == "plan_scenes":
                return self._persist_plan(claimed)
            if run["stage"] == "anchor_visuals":
                return self._validate_anchors(claimed)
            if run["stage"] == "generate_shots":
                return self._queue_shots(claimed)
            if run["stage"] == "inspect_clips":
                return self._inspect(claimed)
            return self._block(claimed, f"Stage '{run['stage']}' has no verified implementation. No export or creative approval was attempted.")
        except (PlannerUnavailable, PlannerOutputError, FilmRunTransitionError, ValueError, OSError) as exc:
            return self._block(claimed, str(exc))
        except Exception as exc:
            return self._block(claimed, f"Film stage failed safely: {exc}")

    def _plan(self, run: dict) -> StoryPlan:
        project = self.db.get_project(run["project_id"])
        if not project or project.get("archived_at"):
            raise PlannerOutputError("The run's project is unavailable; no jobs were queued")
        selected_ids = set(run["reference_ids"])
        catalog = []
        for reference in self.db.project_references(project["id"]):
            if reference["id"] not in selected_ids:
                continue
            path = safe_local_path(reference["path"], self.settings, must_exist=True)
            if (not path.is_file() or not reference.get("mime_type", "").startswith("image/") or
                    self._historical_reference(reference, path)):
                raise PlannerOutputError(f"Selected reference '{reference['name']}' is unavailable or not an approved image")
            catalog.append({"id": reference["id"], "name": reference["name"],
                            "category": reference["category"], "notes": reference.get("notes", ""),
                            "mime_type": reference["mime_type"]})
        if len(catalog) != len(selected_ids):
            raise PlannerOutputError("A selected reference is no longer linked to this project")
        budget = run["budget"]
        request = run["request"]
        planned = self.planner.plan({
            "prompt": run["prompt"], "project_id": project["id"],
            "project_name": project["name"], "project_style_bible": project.get("style_bible", {}),
            "project_aspect_ratio": project.get("aspect_ratio") or "9:16",
            "project_frame_rate": project.get("frame_rate") or 24,
            "project_reference_catalog": catalog,
            "allowed_reference_ids": sorted(selected_ids),
            "shot_budget": budget["max_shots"], "preset": request["preset"],
            "model_capabilities": {"text_to_video": True, "image_to_video": True,
                "max_reference_images_per_shot": 1, "audio_generation": False,
                "video_continuation": False, "ic_lora": False,
                "frames": request.get("generation_profile", {}).get("frames"),
                "fps": request.get("generation_profile", {}).get("fps")},
        })
        categories = {item["id"]: item["category"] for item in catalog}
        return planned.validate_project_scope(selected_ids, categories)

    @staticmethod
    def _historical_reference(reference: dict, path: Path) -> bool:
        name = f"{reference.get('name', '')} {path.name}".casefold()
        return any(word in name for word in ("riverbank-master", "benchmark", "storyboard", "generated", ".mp4", ".mov"))

    def _understand(self, run: dict):
        plan = self._plan(run)
        if plan.audio_requested or plan.video_continuation_requested or plan.ic_lora_requested:
            unsupported = [name for name, requested in (("audio", plan.audio_requested),
                ("video continuation", plan.video_continuation_requested), ("IC-LoRA", plan.ic_lora_requested)) if requested]
            raise PlannerOutputError("The validated plan requires unsupported local capability: " + ", ".join(unsupported) + ". No shot jobs were queued.")
        count = len(plan.ordered_shots)
        if count > run["budget"]["max_shots"]:
            raise FilmRunTransitionError(f"The validated plan has {count} shots, exceeding the authorized {run['budget']['max_shots']} shot limit")
        checkpoint = plan.model_dump(mode="json")
        checkpoint["shot_count"] = count
        frames, fps = self._preset(run["request"]["preset"])
        benchmark = next((item for item in self.db.benchmarks()
                          if item["frames"] == frames and item["fps"] == fps and item.get("elapsed_seconds") is not None), None)
        checkpoint["estimated_generation_seconds"] = round(float(benchmark["elapsed_seconds"]) * count) if benchmark else None
        if checkpoint["estimated_generation_seconds"] is not None and checkpoint["estimated_generation_seconds"] > run["budget"]["max_generation_seconds"]:
            raise FilmRunTransitionError("The measured local generation estimate exceeds this run's time budget")
        return self.db.checkpoint_film_run(run["id"], run["revision"], "understand_story", "define_visual_bible",
                                           checkpoint, status="waiting_for_planner",
                                           message=f"Structured story validated: {len(plan.scenes)} scenes and {count} shots.")

    def _define_style(self, run: dict):
        plan = self._validated_plan(run["checkpoint"]["understand_story"])
        project = self.db.get_project(run["project_id"])
        result = run["checkpoint"]["understand_story"] | {
            "style_bible": project.get("style_bible", {}),
            "planner_visual_style": plan.visual_style,
            "creative_review": "pending after technical generation; no automatic visual quality detector is configured",
        }
        return self.db.checkpoint_film_run(run["id"], run["revision"], "define_visual_bible", "plan_scenes",
                                           result, status="waiting_for_planner",
                                           message="Project style and planner visual direction were recorded for shot prompts.")

    def _persist_plan(self, run: dict):
        plan_data = run["checkpoint"]["define_visual_bible"]
        plan = self._validated_plan(plan_data)
        shots, scenes, metadata = self._storyboard_rows(run, plan)
        enforcement = {"shot_count": len(shots),
                       "estimated_generation_seconds": plan_data.get("estimated_generation_seconds"),
                       "estimated_output_bytes": self._estimated_output_bytes(run, len(shots))}
        enforce_generation_budget(run, enforcement, "plan_scenes")
        storyboard = self.db.create_film_run_plan(
            run["id"], plan.title, run["prompt"], shots, scenes,
            self.db.get_project(run["project_id"]).get("aspect_ratio") or "9:16",
            run["request"]["preset"], metadata)
        result = {"shot_count": len(shots), "estimated_generation_seconds": enforcement["estimated_generation_seconds"],
                  "estimated_output_bytes": enforcement["estimated_output_bytes"],
                  "storyboard_id": storyboard["id"], "storyboard_revision": storyboard["revision"],
                  "scene_count": len(scenes), "title": storyboard["title"]}
        return self.db.checkpoint_film_run(run["id"], run["revision"], "plan_scenes", "anchor_visuals",
                                           result, status="waiting_for_planner",
                                           message=f"Saved validated plan '{storyboard['title']}' with {len(scenes)} scenes; generation is still queued nowhere.")

    def _storyboard_rows(self, run: dict, plan: StoryPlan):
        project = self.db.get_project(run["project_id"])
        request = run["request"]
        frames, preset_fps = self._preset(request["preset"])
        fps = project.get("frame_rate") or preset_fps
        width, height = {"16:9": (768, 432), "1:1": (640, 640), "4:3": (640, 480), "3:2": (672, 448)}.get(project.get("aspect_ratio") or "9:16", (448, 768))
        output = safe_local_path(str(self.settings.expanded(self.settings.output_root) / project["id"]), self.settings)
        output.mkdir(parents=True, exist_ok=True)
        linked = {item["id"]: item for item in self.db.project_references(project["id"])}
        selected = set(request["reference_ids"])
        reference_paths = {}
        for reference_id in selected:
            item = linked.get(reference_id)
            if not item or not item.get("mime_type", "").startswith("image/"):
                raise PlannerOutputError("A selected image reference was removed or changed before plan validation")
            path = safe_local_path(item["path"], self.settings, must_exist=True)
            if self._historical_reference(item, path):
                raise PlannerOutputError("Historical media cannot be reused as a new film reference without separate approval")
            reference_paths[reference_id] = str(path)
        profile = self.db.get_setting(f"generation:{project['id']}") or self.db.get_setting("generation") or {}
        style_bible = project.get("style_bible", {})
        style_lines = [f"{key.replace('_', ' ').title()}: {value}" for key, value in style_bible.items() if isinstance(value, str) and value.strip()]
        characters = {item.id: item for item in plan.characters}
        locations = {item.id: item for item in plan.locations}
        seconds_per_shot = frames / fps
        rows, scene_rows = [], []
        for scene in plan.scenes:
            saved_scene_title = f"{scene.sequence + 1:02d}. {scene.title}"
            scene_rows.append({"title": saved_scene_title,
                               "visual_description": scene.visual_description + " Character layout: " + scene.character_layout + " Framing: " + scene.framing,
                               "estimated_seconds": round(seconds_per_shot * len(scene.shots), 2),
                               "transition_after": scene.shots[-1].transition_after})
            location = locations[scene.location_id]
            for shot in scene.shots:
                reference_id = shot.reference_ids[0] if shot.reference_ids else None
                ref_path = reference_paths.get(reference_id) if reference_id else None
                identity_lines = [f"{characters[cid].name}: {characters[cid].identity}; distinguishing traits: " + ", ".join(characters[cid].distinguishing_traits)
                                  for cid in shot.characters_present]
                prompt = "\n".join([
                    shot.prompt,
                    f"Visual style: {plan.visual_style}",
                    f"Location continuity: {location.name}. {location.description} Scale: {location.scale}",
                    "Character identity continuity: " + " | ".join(identity_lines),
                    "Scene character layout: " + scene.character_layout,
                    "Scene continuity: " + " | ".join(scene.continuity_requirements),
                    "Shot framing: " + shot.framing + ". Shot character layout: " + shot.character_layout,
                    *[f"Project style bible: {line}" for line in style_lines],
                ])
                if len(prompt) > 4000:
                    raise PlannerOutputError(f"The validated prompt for '{shot.title}' exceeds LTX's 4,000 character limit")
                generation = GenerationSettings.model_validate(profile | {
                    "prompt": prompt, "model": profile.get("model", "dgrauet/ltx-2.3-mlx-q8"),
                    "width": width, "height": height, "distilled_two_stage": profile.get("distilled_two_stage", True),
                    "low_ram": profile.get("low_ram", True), "audio": False,
                    "seed": profile.get("seed"), "reference_image": ref_path,
                    "output_location": str(output), "preset": request["preset"],
                })
                payload = generation.model_dump() | {"frames": frames, "fps": fps,
                    "shot_prompt": prompt, "shot_settings": {"width": width, "height": height, "fps": fps,
                        "output_location": str(output), "framing": shot.framing,
                        "style_bible_revision": project.get("style_bible_revision", 0)},
                    "style_context": plan.visual_style, "reference_conditioning": "one-image" if ref_path else "text-only",
                    "reference_id": reference_id, "preflight_warnings": []}
                shot_id = str(uuid5(UUID(run["id"]), f"shot:{shot.id}"))
                rows.append({"id": shot_id, "scene_title": saved_scene_title, "title": shot.title,
                    "prompt": prompt, "visual_description": shot.beat, "reference_image": ref_path,
                    "reference_ids": [reference_id] if reference_id else [], "preset": request["preset"],
                    "seed": generation.seed, "caption": shot.caption, "dialogue": shot.caption,
                    "characters": [characters[cid].name for cid in shot.characters_present],
                    "framing": shot.framing, "width": width, "height": height, "fps": fps,
                    "output_location": str(output), "estimated_seconds": round(seconds_per_shot, 2),
                    "transition_after": shot.transition_after, "generation_payload": payload})
        metadata = {"planner_disclosure": "Structured plan produced by the explicitly configured local planner and validated against the project and run constraints.",
                    "planner_mode": "local-structured-v1", "story_plan": plan.model_dump(mode="json"),
                    "scene_count": len(plan.scenes), "shot_count": len(rows), "preset": request["preset"],
                    "frames_per_shot": frames, "fps": fps, "width": width, "height": height,
                    "approved_reference_ids": sorted(selected),
                    "reference_limit_note": "At most one approved image conditions each shot. Unreferenced characters rely on prompt descriptions; local image conditioning does not guarantee identity consistency.",
                    "creative_quality_status": "pending_manual_review", "audio_available": False,
                    "model_capabilities": {"text_to_video": True, "image_to_video": True,
                        "max_reference_images_per_shot": 1, "audio_generation": False,
                        "video_continuation": False, "ic_lora": False},
                    "style_bible_revision": project.get("style_bible_revision", 0), "style_bible_snapshot": style_bible}
        return rows, scene_rows, metadata

    def _validate_anchors(self, run: dict):
        storyboard_id = run.get("storyboard_id")
        if not storyboard_id or not self.db.get_storyboard(storyboard_id, include_events=False):
            raise FilmRunTransitionError("The validated project storyboard is missing; no shots were queued")
        return self.db.checkpoint_film_run(run["id"], run["revision"], "anchor_visuals", "generate_shots",
                                           {"storyboard_id": storyboard_id, "approved_reference_ids": run["reference_ids"]},
                                           status="waiting_for_planner", message="Project-linked reference IDs and generated path scope were rechecked.")

    def _queue_shots(self, run: dict):
        if not self.ltx_ready():
            raise PlannerUnavailable("LTX runner or cached Q8 weights are unavailable. No model download was attempted and no shot jobs were queued.")
        output_root = safe_local_path(str(self.settings.expanded(self.settings.output_root)), self.settings)
        output_root.mkdir(parents=True, exist_ok=True)
        available_bytes = shutil.disk_usage(output_root).free
        if available_bytes < run["budget"]["max_output_bytes"]:
            raise PlannerUnavailable(f"Free disk space changed after authorization ({available_bytes} bytes); it is below this run's output cap. No shot jobs were queued.")
        story = self.db.get_storyboard(run["storyboard_id"], include_events=False)
        if not story or story["project_id"] != run["project_id"] or not story["film_plan"]["approved"]:
            raise FilmRunTransitionError("The run's validated project plan is unavailable or stale; no jobs were queued")
        batch = self.db.create_film_batch(story["id"], story["revision"], story["style_bible_revision"], film_run_id=run["id"])
        return self.db.checkpoint_film_run(run["id"], run["revision"], "generate_shots", "inspect_clips",
                                           {"storyboard_id": story["id"], "film_batch_id": batch["id"],
                                            "queued_shots": batch["queued_shots"], "total_shots": batch["total_shots"]},
                                           status="waiting_for_planner",
                                           message=f"Validated plan connected to the single local queue; {batch['queued_shots']} shot job(s) queued.")

    def _inspect(self, run: dict):
        batch = self._batch_for(run)
        if not batch:
            raise FilmRunTransitionError("The film run has no durable queue association")
        if batch["status"] == "completed" and batch["completed_shots"] == batch["total_shots"]:
            return self.db.checkpoint_film_run(run["id"], run["revision"], "inspect_clips", "creative_review",
                {"film_batch_id": batch["id"], "verified_shots": batch["completed_shots"],
                 "total_shots": batch["total_shots"], "creative_review": "pending"},
                status="needs_review", message="All clips passed technical verification. Creative quality is still pending human review; no composition or render was started.")
        if batch["status"] in {"failed", "cancelled"}:
            return self._block(run, f"The shot batch is {batch['status']}; generated files and job logs were preserved. Review failed shots before retrying.")
        if batch["status"] == "paused":
            return self._block(run, "The shot batch is paused. Resume the film run after reviewing its persisted shot queue; no files were removed.")
        if batch["status"] == "completed":
            return self._block(run, "The shot batch ended without every clip passing technical verification. Creative approval was not granted.")
        return None

    def _batch_for(self, run: dict):
        batch_id = run.get("film_batch_id") or run.get("checkpoint", {}).get("generate_shots", {}).get("film_batch_id")
        return self.db.get_film_batch(batch_id) if batch_id else None

    def _block(self, run: dict, message: str):
        current = self.db.get_film_run(run["id"])
        if current["status"] != "running":
            return current
        return self.db.update_film_run(run["id"], current["revision"], status="needs_attention",
                                       error=message[:2000], message=message[:2000])

    def _preset(self, name: str) -> tuple[int, int]:
        preset = next((row for row in BUILTIN_PRESETS if row.name == name), None)
        if preset:
            return preset.frames, preset.fps
        custom = self.db.get_setting("custom_presets") or []
        found = next((row for row in custom if row.get("name") == name), None)
        if not found:
            raise FilmRunTransitionError(f"Unknown generation preset '{name}'")
        return int(found["frames"]), int(found["fps"])

    @staticmethod
    def _validated_plan(data: dict) -> StoryPlan:
        return StoryPlan.model_validate({key: data[key] for key in StoryPlan.model_fields if key in data})

    def _estimated_output_bytes(self, run: dict, shot_count: int) -> int | None:
        frames, fps = self._preset(run["request"]["preset"])
        sizes = []
        for job in self.db.list_jobs(project_id=run["project_id"], limit=1000):
            verified = (job.get("verification_metadata") or {}).get("verified_output") or {}
            path = Path(job["result_path"]) if job.get("result_path") else None
            if (job["kind"] == "generation" and job["status"] == "completed" and path and path.is_file()
                    and verified.get("frames") == frames and verified.get("fps") == fps):
                try:
                    sizes.append(path.stat().st_size)
                except OSError:
                    pass
        return int(median(sizes) * shot_count) if sizes else None
