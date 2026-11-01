"""Google Gemini adapter for the provider-neutral, strict FRAMEWORK StoryPlan."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .story_planning import PlannerOutputError, PlannerResult, StoryPlan, StoryPlanner

MAX_OUTPUT_TOKENS = 8192
MIN_OUTPUT_TOKENS = 3072
OUTPUT_TOKENS_PER_SHOT = 640
MAX_STORY_SCENES = 40
MAX_SHOTS_PER_SCENE = 40
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SCHEMA_REPAIR_REQUESTS = 1
STORY_PLAN_SCHEMA_VERSION = "storyplan-budgeted-v1"
GEMINI_API_VERSION = "v1beta"
GENERATION_ENDPOINT_PROBE_PROMPT = "Reply with exactly: FRAMEWORK Gemini endpoint check passed."
_FLASH_ALLOWLIST = re.compile(r"^(?:models/)?gemini-[a-z0-9][a-z0-9.-]*flash[a-z0-9.-]*$", re.IGNORECASE)


class GeminiPlanningError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable_by_user: bool = False,
                 diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable_by_user = retryable_by_user
        self.diagnostics = diagnostics or {}


def _client(api_key: str, timeout_seconds: int):
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GeminiPlanningError("sdk_missing", "The Google GenAI Python SDK is not installed in this backend environment.") from exc
    # Let the pinned Google SDK select the Gemini Developer API base URL and
    # default v1beta version. Overrides duplicate SDK routing and can diverge
    # from account model discovery as the SDK evolves.
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(
        timeout=timeout_seconds * 1000,
        retry_options=types.HttpRetryOptions(attempts=1),
    ))


def _model_fields(model) -> tuple[str, str, tuple[str, ...]]:
    if isinstance(model, dict):
        name = model.get("name") or model.get("model") or ""
        display = model.get("display_name") or model.get("displayName") or name
        actions = (model.get("supported_actions") or model.get("supportedActions") or
                   model.get("supported_generation_methods") or model.get("supportedGenerationMethods") or [])
    else:
        name = getattr(model, "name", "") or getattr(model, "model", "")
        display = getattr(model, "display_name", None) or getattr(model, "displayName", None) or name
        actions = (getattr(model, "supported_actions", None) or getattr(model, "supportedActions", None) or
                   getattr(model, "supported_generation_methods", None) or
                   getattr(model, "supportedGenerationMethods", None) or [])
    normalized = str(name).removeprefix("models/")
    return normalized, str(display), tuple(str(item) for item in actions)


def _safe_usage(response) -> dict[str, int | None]:
    usage = getattr(response, "usage_metadata", None) or getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump(by_alias=False, exclude_none=True)
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    return {"input_tokens": usage.get("prompt_token_count", usage.get("total_input_tokens")),
            "output_tokens": usage.get("candidates_token_count", usage.get("total_output_tokens")),
            "total_tokens": usage.get("total_token_count", usage.get("total_tokens"))}


def gemini_story_schema(shot_budget: int = MAX_STORY_SCENES) -> dict[str, Any]:
    """Return a compact JSON Schema with array caps based on the authorized budget.

    JSON Schema cannot express a sum over nested shot arrays. Both scene count
    and per-scene shot count are bounded by the run budget; local StoryPlan
    validation still enforces the exact total.
    """
    try:
        shot_limit = max(1, min(int(shot_budget), MAX_STORY_SCENES * MAX_SHOTS_PER_SCENE))
    except (TypeError, ValueError):
        shot_limit = 1
    scene_limit = min(MAX_STORY_SCENES, shot_limit)
    shots_per_scene_limit = min(MAX_SHOTS_PER_SCENE, shot_limit)
    location_limit = min(20, shot_limit)

    def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def string(description: str = "") -> dict[str, Any]:
        return {"type": "string", **({"description": description} if description else {})}

    def array(item: dict[str, Any], maximum: int, minimum: int = 0) -> dict[str, Any]:
        return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}

    character = obj({"id": string(), "name": string(), "identity": string("Stable visual identity; preserve these attributes in every shot."),
                     "distinguishing_traits": array(string(), 12, 1), "reference_ids": array(string(), 16)},
                    ["id", "name", "identity", "distinguishing_traits", "reference_ids"])
    location = obj({"id": string(), "name": string(), "description": string(), "scale": string(),
                    "continuity_requirements": array(string(), 12, 1)},
                   ["id", "name", "description", "scale", "continuity_requirements"])
    shot = obj({"id": string(), "sequence": {"type": "integer", "minimum": 0}, "title": string(), "beat": string(),
                "prompt": string(), "framing": string(), "character_layout": string(),
                "characters_present": array(string(), 20, 1),
                "expected_character_count": {"type": "integer", "minimum": 1, "maximum": 20},
                "reference_ids": array(string(), 1), "caption": string(),
                "transition_after": {"type": "string", "enum": ["cut", "dissolve", "fade"]}},
               ["id", "sequence", "title", "beat", "prompt", "framing", "character_layout", "characters_present",
                "expected_character_count", "reference_ids", "caption", "transition_after"])
    scene = obj({"id": string(), "sequence": {"type": "integer", "minimum": 0}, "title": string(), "story_beat": string(),
                 "location_id": string(), "characters_present": array(string(), 20, 1), "visual_description": string(),
                 "framing": string(), "character_layout": string(), "continuity_requirements": array(string(), 16, 1),
                 "expected_character_count": {"type": "integer", "minimum": 1, "maximum": 20},
                 "shots": array(shot, shots_per_scene_limit, 1)},
                ["id", "sequence", "title", "story_beat", "location_id", "characters_present", "visual_description",
                 "framing", "character_layout", "continuity_requirements", "expected_character_count", "shots"])
    return obj({"title": string(), "logline": string(), "visual_style": string(), "audio_requested": {"type": "boolean"},
                "video_continuation_requested": {"type": "boolean"}, "ic_lora_requested": {"type": "boolean"},
                "characters": array(character, 20, 1), "locations": array(location, location_limit, 1), "scenes": array(scene, scene_limit, 1)},
               ["title", "logline", "visual_style", "audio_requested", "video_continuation_requested", "ic_lora_requested",
                "characters", "locations", "scenes"])


def _output_token_limit(shot_budget: int) -> int:
    """Scale response size with the authorized shot count, with bounded limits."""
    try:
        shot_limit = max(1, int(shot_budget))
    except (TypeError, ValueError):
        shot_limit = 1
    return min(MAX_OUTPUT_TOKENS, MIN_OUTPUT_TOKENS + OUTPUT_TOKENS_PER_SHOT * shot_limit)


class GeminiStoryPlanner(StoryPlanner):
    provider = "gemini"

    def __init__(self, api_key: str | None, model: str | None, timeout_seconds: int = 120, client_factory=None):
        self.api_key, self.model = api_key, model
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory or _client

    @staticmethod
    def available_models(client) -> list[dict[str, str]]:
        try:
            models = client.models.list()
            result = []
            for model in models:
                name, display, actions = _model_fields(model)
                compatible = {action.replace("_", "").replace("-", "").casefold() for action in actions}
                if (name and _FLASH_ALLOWLIST.fullmatch(name) and "generatecontent" in compatible
                        and "image" not in name.casefold()):
                    result.append({"id": name, "display_name": display})
            return sorted(result, key=lambda item: item["id"].casefold())
        except Exception as exc:
            raise GeminiStoryPlanner._classify_error(exc, "connection") from None

    @classmethod
    def _classify_error(cls, exc: Exception, context: str, *, response_mode: str = "schema") -> GeminiPlanningError:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if hasattr(code, "value"):
            code = code.value
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = None
        class_name = type(exc).__name__.casefold()
        safe_context = {"connection": "Gemini connection test",
                        "generation endpoint probe": "Gemini basic generation endpoint test"}.get(context, "Gemini story planning")
        diagnostics = cls._safe_provider_diagnostics(exc, status)
        if context == "story planning":
            diagnostics["response_mode"] = response_mode
        if status in (401, 403):
            return GeminiPlanningError("auth_denied", f"{safe_context} was denied. Check the Gemini API key, API access, and account permissions.", diagnostics=diagnostics)
        if status == 429:
            return GeminiPlanningError("rate_limit", f"Gemini rate or quota limit reached during {context}. Review the provider account quota; no application cooldown is imposed and no automatic retry was made.", retryable_by_user=True, diagnostics=diagnostics)
        if status in (502, 503, 504):
            return GeminiPlanningError("service_unavailable", f"Gemini is temporarily unavailable during {context}; no automatic retry was made.", retryable_by_user=True, diagnostics=diagnostics)
        if status == 400:
            fields = diagnostics.get("field_paths", [])
            model_field = any("model" in field.casefold() for field in fields)
            schema_field = any(any(term in field.casefold() for term in ("schema", "responsejsonschema", "response_json_schema")) for field in fields)
            if model_field:
                diagnostics["category"] = "model_or_endpoint_rejected"
                message = "Gemini rejected the selected model or model parameter (HTTP 400). Check the model listed by this account."
                error_code = "model_parameter_rejected"
            elif context == "story planning" and response_mode == "schema" and (schema_field or not fields):
                diagnostics["category"] = "response_schema_rejected"
                message = "Gemini rejected the structured JSON Schema request (HTTP 400). The model may be valid while a schema element or schema-mode parameter is unsupported. Select JSON-only format to retry explicitly without provider-side schema enforcement."
                error_code = "schema_rejected"
            else:
                diagnostics["category"] = "request_parameter_rejected"
                message = "Gemini rejected a request parameter (HTTP 400). The sanitized field path below identifies the rejected parameter when Google provided one."
                error_code = "invalid_request"
            message += cls._diagnostic_suffix(diagnostics)
            return GeminiPlanningError(error_code, message, retryable_by_user=True, diagnostics=diagnostics)
        if status == 404:
            diagnostics["category"] = "model_or_endpoint_incompatible"
            if context == "generation endpoint probe":
                message = ("The basic Gemini generateContent request returned HTTP 404. This request contained only "
                           "the fixed diagnostic text and no schema or media. Check the API key's Google project, "
                           "Gemini API access, and current availability of the account-listed model.")
            else:
                message = ("Gemini returned 404 for structured generation. The selected model may not support the "
                           "generateContent API, may no longer be available, or may be incompatible with this API version.")
            return GeminiPlanningError("model_or_endpoint_incompatible",
                message + cls._diagnostic_suffix(diagnostics),
                retryable_by_user=True, diagnostics=diagnostics)
        if "timeout" in class_name or isinstance(exc, TimeoutError):
            return GeminiPlanningError("timeout", "Gemini did not finish before the configured request timeout. Its remote outcome is unknown; the run will not resend automatically.", retryable_by_user=True)
        if "blocked" in class_name or "safety" in class_name:
            return GeminiPlanningError("blocked_content", "Gemini blocked this story-planning request under its safety policy. Review the prompt and retry only if appropriate.", retryable_by_user=True)
        if "connection" in class_name or "transport" in class_name:
            return GeminiPlanningError("service_unavailable", f"Gemini could not be reached during {context}. Check network access; the run will not resend automatically after restart.", retryable_by_user=True, diagnostics=diagnostics)
        return GeminiPlanningError("provider_error", f"Gemini {context} failed ({type(exc).__name__}). Provider response text was omitted to protect prompt and credential privacy.", retryable_by_user=True, diagnostics=diagnostics)

    @staticmethod
    def _safe_provider_diagnostics(exc: Exception, http_status: int | None) -> dict[str, Any]:
        """Extract only whitelisted machine-readable fields; never expose body text."""
        details = getattr(exc, "details", None)
        error = details.get("error", details) if isinstance(details, dict) else {}
        if not isinstance(error, dict):
            error = {}
        status = error.get("status") or getattr(exc, "status", None)
        safe_token = re.compile(r"^[A-Za-z0-9_.:/-]{1,96}$")
        safe_reason = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")
        provider_status = status if isinstance(status, str) and safe_reason.fullmatch(status) else None
        reasons: list[str] = []
        fields: list[str] = []
        nested = error.get("details", [])
        if isinstance(nested, list):
            for item in nested:
                if not isinstance(item, dict):
                    continue
                reason = item.get("reason")
                if isinstance(reason, str) and safe_reason.fullmatch(reason):
                    reasons.append(reason)
                violations = item.get("fieldViolations", item.get("field_violations", []))
                if isinstance(violations, list):
                    for violation in violations:
                        if not isinstance(violation, dict):
                            continue
                        field = violation.get("field") or violation.get("fieldPath") or violation.get("field_path")
                        known_field_path = isinstance(field, str) and field.startswith((
                            "generationConfig.", "generation_config.", "generateContentRequest.",
                            "contents", "model", "tools.", "systemInstruction.", "system_instruction."))
                        if known_field_path and safe_token.fullmatch(field):
                            fields.append(field)
        reason = reasons[0] if reasons else (provider_status if provider_status and provider_status != "INVALID_ARGUMENT" else None)
        result: dict[str, Any] = {}
        if http_status is not None:
            result["http_status"] = http_status
        if provider_status:
            result["provider_status"] = provider_status
        if reason:
            result["reason"] = reason
        if fields:
            result["field_paths"] = sorted(set(fields))[:8]
        return result

    @staticmethod
    def _diagnostic_suffix(diagnostics: dict[str, Any]) -> str:
        parts = []
        if diagnostics.get("provider_status"):
            parts.append(f"status={diagnostics['provider_status']}")
        if diagnostics.get("reason"):
            parts.append(f"reason={diagnostics['reason']}")
        if diagnostics.get("field_paths"):
            parts.append("fields=" + ", ".join(diagnostics["field_paths"]))
        return (" Safe provider diagnostics: " + "; ".join(parts) + ".") if parts else ""

    def _require_key(self):
        if not self.api_key:
            raise GeminiPlanningError("missing_key", "Gemini API key is missing. Connect Gemini in Settings, then resume this run.")
        if not self.model:
            raise GeminiPlanningError("model_missing", "No account-verified Gemini Flash model is selected. Test the Gemini connection and choose an available model in Settings.")
        if not _FLASH_ALLOWLIST.fullmatch(self.model):
            raise GeminiPlanningError("model_invalid", "The selected Gemini model is not a supported Flash-class model from the verified account model list.")

    def test_connection(self) -> list[dict[str, str]]:
        self._require_key_for_test()
        try:
            return self.available_models(self.client_factory(self.api_key, self.timeout_seconds))
        except GeminiPlanningError:
            raise
        except Exception as exc:
            raise self._classify_error(exc, "connection") from None

    def test_generation_endpoint(self) -> dict[str, Any]:
        """Send one fixed text-only request through the same SDK route as planning."""
        self._require_key()
        try:
            client = self.client_factory(self.api_key, self.timeout_seconds)
            http_options = getattr(client._api_client, "_http_options", None)
            api_version = getattr(http_options, "api_version", None) or GEMINI_API_VERSION
            base_url = getattr(http_options, "base_url", None) or "https://generativelanguage.googleapis.com/"
            from urllib.parse import urlsplit
            host = urlsplit(base_url).hostname or "generativelanguage.googleapis.com"
            model_id = self.model.removeprefix("models/")
            route = f"/{api_version}/models/{model_id}:generateContent"
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=GENERATION_ENDPOINT_PROBE_PROMPT,
                    config={"max_output_tokens": 24},
                )
            except Exception as exc:
                error = self._classify_error(exc, "generation endpoint probe")
                error.diagnostics.update({"model": model_id, "api_version": api_version,
                                          "endpoint_host": host, "endpoint_path": route,
                                          "request_shape": "text_only_no_schema_no_media"})
                if error.code == "model_or_endpoint_incompatible":
                    error.args = ("The account-listed model's basic generateContent request returned 404. "
                                  "The request used the pinned SDK route shown below; schema settings were not sent. "
                                  "Check that the Gemini API is enabled for the API key's project and confirm "
                                  "the listed model is available to this key." + self._diagnostic_suffix(error.diagnostics),)
                raise error from None
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise GeminiPlanningError("empty_generation_response",
                    "Gemini accepted the basic generateContent request but returned no text. Check provider safety feedback and account access.",
                    retryable_by_user=True,
                    diagnostics={"http_status": 200, "model": model_id, "api_version": api_version,
                                 "endpoint_host": host, "endpoint_path": route,
                                 "request_shape": "text_only_no_schema_no_media"})
            return {"success": True, "http_status": 200, "model": model_id,
                    "api_version": api_version, "endpoint_host": host,
                    "endpoint_path": route, "request_shape": "text_only_no_schema_no_media",
                    "response_text": text.strip()[:240]}
        except GeminiPlanningError:
            raise
        except Exception as exc:
            raise self._classify_error(exc, "generation endpoint probe") from None

    def _require_key_for_test(self):
        if not self.api_key:
            raise GeminiPlanningError("missing_key", "Gemini API key is missing. Connect it in Settings first.")

    @staticmethod
    def _validate_output(text: str, request: dict[str, Any]) -> StoryPlan:
        if not text or not text.strip():
            raise PlannerOutputError("Gemini returned empty structured output")
        if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise PlannerOutputError("Gemini StoryPlan output exceeded the 8 MiB safety limit")
        plan = StoryPlan.model_validate_json(text)
        if len(plan.ordered_shots) > int(request["shot_budget"]):
            raise PlannerOutputError("Gemini StoryPlan exceeds the authorized maximum shot count")
        categories = {item["id"]: item["category"] for item in request.get("project_reference_catalog", [])}
        return plan.validate_project_scope(set(request.get("allowed_reference_ids", [])), categories)

    @staticmethod
    def _privacy_filtered_request(request: dict[str, Any]) -> dict[str, Any]:
        capabilities = request.get("model_capabilities") or {}
        allowed_capabilities = {key: capabilities[key] for key in (
            "text_to_video", "image_to_video", "max_reference_images_per_shot", "audio_generation",
            "video_continuation", "ic_lora", "frames", "fps") if key in capabilities}
        catalog = [{"id": item["id"], "category": item["category"]}
                   for item in request.get("project_reference_catalog", [])
                   if isinstance(item, dict) and item.get("id") and item.get("category")]
        return {
            "prompt": request.get("prompt", ""),
            "project_style_bible": request.get("project_style_bible", {}),
            "project_aspect_ratio": request.get("project_aspect_ratio"),
            "project_frame_rate": request.get("project_frame_rate"),
            "project_reference_catalog": catalog,
            "allowed_reference_ids": [item["id"] for item in catalog],
            "reference_privacy": "Opaque IDs and categories only. No images, filenames, notes, or paths are sent.",
            "shot_budget": request.get("shot_budget"),
            "budget": request.get("budget", {}),
            "preset": request.get("preset"),
            "model_capabilities": allowed_capabilities,
        }

    @staticmethod
    def _prompt(request: dict[str, Any], repair: dict[str, str] | None = None) -> str:
        input_json = json.dumps(GeminiStoryPlanner._privacy_filtered_request(request), ensure_ascii=False, separators=(",", ":"))
        prompt = ("Create a film story plan using the user's prompt as the only plot source. Preserve distinct character identities, ")
        prompt += ("causal chronology, location scale, visual continuity, camera framing, and exactly described character layout. "
                   "Use only supplied opaque reference IDs; the planner cannot see reference images, names, or paths. Leave references unassigned when their identity is ambiguous. "
                   "Do not invent approvals. Keep within the exact shot budget, preset, and local model capabilities. Set unsupported audio, continuation, and IC-LoRA flags accurately. "
                   "Return a StoryPlan JSON object.\nINPUT_JSON:\n" + input_json)
        if repair:
            prompt += "\n\nThe prior response failed local validation. Repair it, preserving story facts. Validation error: " + repair["error"] + "\nINVALID_RESPONSE_JSON:\n" + repair["output"][:MAX_RESPONSE_BYTES]
        return prompt

    def _call(self, client, request_text: str, shot_budget: int, response_mode: str):
        config = {"response_mime_type": "application/json",
                  "max_output_tokens": _output_token_limit(shot_budget)}
        if response_mode == "schema":
            config["response_json_schema"] = gemini_story_schema(shot_budget)
        elif response_mode != "json_only":
            raise GeminiPlanningError("invalid_response_mode", "Unsupported Gemini response format")
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=request_text,
                # JSON-only mode omits the provider schema when a model
                # rejects it; strict local StoryPlan validation is unchanged.
                config=config,
            )
            prompt_feedback = getattr(response, "prompt_feedback", None)
            block_reason = getattr(prompt_feedback, "block_reason", None) if prompt_feedback else None
            block_reason = getattr(block_reason, "name", block_reason)
            if block_reason and str(block_reason).upper() not in {"UNSPECIFIED", "BLOCK_REASON_UNSPECIFIED", "0"}:
                raise GeminiPlanningError("blocked_content", "Gemini blocked this story-planning request under its safety policy. Review the prompt and retry only if appropriate.", retryable_by_user=True)
            candidates = getattr(response, "candidates", None) or []
            finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
            finish_reason = getattr(finish_reason, "name", finish_reason)
            if str(finish_reason).upper() in {"MAX_TOKENS", "MAX_TOKENS_REACHED"}:
                raise GeminiPlanningError("incomplete_output", "Gemini reached its output-token limit before completing the StoryPlan. Increase the story budget or simplify the request.", retryable_by_user=True)
            if str(finish_reason).upper() in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT"}:
                raise GeminiPlanningError("blocked_content", "Gemini blocked this story-planning request under its safety policy. Review the prompt and retry only if appropriate.", retryable_by_user=True)
            return response
        except GeminiPlanningError:
            raise
        except Exception as exc:
            raise self._classify_error(exc, "story planning", response_mode=response_mode) from None

    def plan(self, request: dict[str, Any]) -> PlannerResult:
        self._require_key()
        if len(str(request.get("prompt", "")).encode("utf-8")) > 20_000:
            raise GeminiPlanningError("prompt_too_large", "Story prompt exceeds the 20,000 character planning limit.")
        if len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
            raise GeminiPlanningError("metadata_too_large", "Story-planning metadata exceeded the 64 KiB request cap.")
        try:
            client = self.client_factory(self.api_key, self.timeout_seconds)
        except GeminiPlanningError:
            raise
        except Exception as exc:
            raise self._classify_error(exc, "story planning") from None
        shot_budget = int(request["shot_budget"])
        response_mode = request.get("response_mode", "schema")
        response = self._call(client, self._prompt(request), shot_budget, response_mode)
        usages = [_safe_usage(response)]
        output = getattr(response, "text", None)
        output = output if isinstance(output, str) else ""
        try:
            plan = self._validate_output(output, request)
        except (PlannerOutputError, ValidationError) as first_error:
            # Exactly one bounded schema repair, charged against this run's two-call cap.
            if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise GeminiPlanningError("invalid_output", "Gemini returned malformed or oversized JSON. No shot jobs were queued.", retryable_by_user=True) from None
            repaired = self._call(client, self._prompt(request, {"error": str(first_error)[:2000], "output": output}), shot_budget, response_mode)
            usages.append(_safe_usage(repaired))
            try:
                plan = self._validate_output(getattr(repaired, "text", None), request)
            except (PlannerOutputError, ValidationError):
                raise GeminiPlanningError("invalid_output", "Gemini output remained invalid after one bounded repair request. No shot jobs were queued.", retryable_by_user=True) from None
        usage = {key: sum((item[key] or 0) for item in usages) if any(item[key] is not None for item in usages) else None
                 for key in ("input_tokens", "output_tokens", "total_tokens")}
        return PlannerResult(plan=plan, provenance={"provider": "gemini", "model": self.model,
            "api": "Gemini generateContent API JSON Schema" if response_mode == "schema" else "Gemini generateContent API JSON-only fallback",
            "response_mode": response_mode, "sdk": "google-genai", "request_count": len(usages),
            "output_token_limit": _output_token_limit(shot_budget),
            "usage": usage, "cost_estimate": None,
            "billing_note": "Gemini billing/free-tier data policy varies by account and tier; review current Google AI Studio pricing.",
            "privacy": "Only story prompt, project style text, preset/budget/capability metadata, and opaque selected-reference IDs/categories were sent. No files or filenames were uploaded."})
