"""Google Gemini adapter for the provider-neutral, strict FRAMEWORK StoryPlan."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .story_planning import PlannerOutputError, PlannerResult, StoryPlan, StoryPlanner

MAX_OUTPUT_TOKENS = 8192
MIN_OUTPUT_TOKENS = 6144
OUTPUT_TOKENS_PER_SHOT = 640
ENDPOINT_PROBE_MAX_OUTPUT_TOKENS = 512
MAX_STORY_SCENES = 40
MAX_SHOTS_PER_SCENE = 40
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SCHEMA_REPAIR_REQUESTS = 1
STORY_PLAN_SCHEMA_VERSION = "storyplan-budgeted-v1"
GEMINI_API_VERSION = "v1beta"
GENERATION_ENDPOINT_PROBE_PROMPT = "Reply with exactly: FRAMEWORK Gemini endpoint check passed."
_SUPPORTED_THINKING_MODEL = re.compile(r"^gemini-(?:3(?:\.\d+)?|2\.5)-", re.IGNORECASE)
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
    name = _model_value(model, "name", "model") or ""
    display = _model_value(model, "display_name", "displayName") or name
    actions = (_model_value(model, "supported_actions", "supportedActions") or
               _model_value(model, "supported_generation_methods", "supportedGenerationMethods") or [])
    normalized = str(name).removeprefix("models/")
    return normalized, str(display), tuple(str(item) for item in actions)


def _model_value(model, *names):
    """Read snake/camel metadata from dicts, SDK objects, or future SDK fields."""
    for name in names:
        if isinstance(model, dict) and name in model:
            return model[name]
        value = getattr(model, name, None)
        if value is not None:
            return value
    if hasattr(model, "model_dump"):
        try:
            dumped = model.model_dump(by_alias=True, exclude_none=True)
        except Exception:
            dumped = {}
        for name in names:
            if name in dumped:
                return dumped[name]
    extras = getattr(model, "__pydantic_extra__", None)
    if isinstance(extras, dict):
        for name in names:
            if name in extras:
                return extras[name]
    return None


def _output_modalities(model) -> tuple[str, ...] | None:
    value = _model_value(model, "output_modalities", "outputModalities", "supported_output_modalities",
                         "supportedOutputModalities", "response_modalities", "responseModalities")
    if value is None:
        return None
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value.strip())
    if not isinstance(value, (list, tuple, set)):
        return None
    modalities = []
    for item in value:
        normalized = getattr(item, "value", None) or getattr(item, "name", None) or item
        if isinstance(normalized, str) and normalized.strip():
            modalities.append(normalized.strip().upper().replace("MODALITY_", ""))
    return tuple(sorted(set(modalities)))


def _text_output_eligibility(model_id: str, modalities: tuple[str, ...] | None) -> tuple[bool, str]:
    """Reject non-story model families; prefer explicit text output metadata."""
    tokens = set(re.split(r"[^a-z0-9]+", model_id.casefold()))
    unsuitable = {"tts", "image", "imagen", "audio", "live", "embedding", "embed",
                  "video", "veo", "music", "lyria", "robotics"}
    if tokens & unsuitable:
        return False, "name_exclusion"
    if modalities is not None:
        return ("TEXT" in modalities, "output_modalities")
    return True, "name_fallback"


def _stored_model_is_text_eligible(model: dict) -> bool:
    model_id = str(model.get("id") or model.get("name") or "")
    modality_value = model.get("output_modalities")
    modalities = (tuple(str(item).upper() for item in modality_value)
                  if isinstance(modality_value, list) and model.get("text_output_source") != "name_fallback" else None)
    return _text_output_eligibility(model_id, modalities)[0]


def _response_output_modalities(response) -> tuple[str, ...]:
    modalities: set[str] = set()

    def value(obj, *keys):
        for key in keys:
            if isinstance(obj, dict) and key in obj:
                return obj[key]
            item = getattr(obj, key, None)
            if item is not None:
                return item
        return None

    candidates = value(response, "candidates") or []
    for candidate in candidates:
        content = value(candidate, "content")
        for part in value(content, "parts") or []:
            text = value(part, "text")
            if isinstance(text, str) and text.strip():
                modalities.add("TEXT")
            inline = value(part, "inline_data", "inlineData")
            mime_type = value(inline, "mime_type", "mimeType")
            if isinstance(mime_type, str):
                kind = mime_type.partition("/")[0].upper()
                if kind in {"AUDIO", "IMAGE", "VIDEO", "TEXT"}:
                    modalities.add(kind)
            for key, kind in (("audio", "AUDIO"), ("output_audio", "AUDIO"),
                              ("outputAudio", "AUDIO"), ("image", "IMAGE"), ("video", "VIDEO")):
                if value(part, key) is not None:
                    modalities.add(kind)
    if not modalities:
        try:
            text = response.text
        except Exception:
            text = None
        if isinstance(text, str) and text.strip():
            modalities.add("TEXT")
    return tuple(sorted(modalities))


def _safe_usage(response) -> dict[str, int | None]:
    usage = getattr(response, "usage_metadata", None) or getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": None, "output_tokens": None, "thoughts_tokens": None, "total_tokens": None}
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump(by_alias=False, exclude_none=True)
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "thoughts_tokens": None, "total_tokens": None}
    def count(*keys):
        value = next((usage[key] for key in keys if key in usage), None)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return {"input_tokens": count("prompt_token_count", "total_input_tokens"),
            "output_tokens": count("candidates_token_count", "total_output_tokens"),
            "thoughts_tokens": count("thoughts_token_count", "total_thought_tokens"),
            "total_tokens": count("total_token_count", "total_tokens")}


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


def _thinking_config_for_model(model: str | None) -> dict[str, str] | None:
    """Use supported low thinking for Gemini 3.x/2.5; leave older families untouched."""
    normalized = str(model or "").removeprefix("models/")
    return {"thinking_level": "low"} if _SUPPORTED_THINKING_MODEL.match(normalized) else None


def _safe_enum(value, allowed: set[str]) -> str | None:
    if value is None:
        return None
    normalized = getattr(value, "name", None) or getattr(value, "value", None) or value
    if not isinstance(normalized, str):
        return None
    token = normalized.upper().rsplit(".", 1)[-1]
    for prefix in ("FINISH_REASON_", "BLOCK_REASON_"):
        token = token.removeprefix(prefix)
    return token if token in allowed else None


_SAFE_FINISH_REASONS = {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "LANGUAGE", "OTHER", "BLOCKLIST",
                        "PROHIBITED_CONTENT", "SPII", "MALFORMED_FUNCTION_CALL", "MALFORMED_RESPONSE",
                        "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS", "CONTINUATION", "IMAGE_SAFETY",
                        "IMAGE_PROHIBITED_CONTENT", "IMAGE_OTHER", "NO_IMAGE", "IMAGE_RECITATION"}
_SAFE_BLOCK_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "OTHER", "IMAGE_SAFETY"}


def _safe_response_metadata(response, *, max_output_tokens: int | None = None) -> dict[str, Any]:
    """Return only safe enums and numeric usage; never include provider text/thoughts."""
    candidates = getattr(response, "candidates", None) or []
    finish_reasons = []
    for candidate in candidates[:4]:
        finish_reason = _safe_enum(getattr(candidate, "finish_reason", None), _SAFE_FINISH_REASONS)
        if finish_reason:
            finish_reasons.append(finish_reason)
    prompt_feedback = getattr(response, "prompt_feedback", None)
    block_reason = _safe_enum(getattr(prompt_feedback, "block_reason", None), _SAFE_BLOCK_REASONS)
    usage = _safe_usage(response)
    metadata: dict[str, Any] = {
        "finish_reasons": finish_reasons,
        "output_modalities": list(_response_output_modalities(response)),
        "prompt_token_count": usage["input_tokens"],
        "candidates_token_count": usage["output_tokens"],
        "thoughts_token_count": usage["thoughts_tokens"],
        "total_token_count": usage["total_tokens"],
    }
    if block_reason:
        metadata["prompt_block_reason"] = block_reason
    if max_output_tokens is not None:
        metadata["max_output_tokens"] = max_output_tokens
    return {key: value for key, value in metadata.items() if value not in (None, [], {})}


def _response_failure(response, *, context: str, max_output_tokens: int) -> GeminiPlanningError | None:
    metadata = _safe_response_metadata(response, max_output_tokens=max_output_tokens)
    candidates = getattr(response, "candidates", None) or []
    finish_reason = metadata.get("finish_reasons", [None])[0]
    block_reason = metadata.get("prompt_block_reason")
    text = None
    try:
        text = response.text
    except Exception:
        pass
    if block_reason or finish_reason in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT"}:
        metadata["category"] = "safety_blocking"
        message = "Gemini blocked this request under its safety policy. Review the story prompt or probe context, then retry only with explicit authorization."
        return GeminiPlanningError("blocked_content", message, retryable_by_user=True, diagnostics=metadata)
    if finish_reason in {"MAX_TOKENS", "CONTINUATION"}:
        metadata["category"] = "output_token_exhaustion"
        thoughts = metadata.get("thoughts_token_count")
        thought_detail = f" The response reports {thoughts} thinking tokens before visible output." if thoughts is not None else ""
        if context == "generation endpoint probe":
            message = (f"Gemini returned HTTP 200 but stopped with {finish_reason} at the {max_output_tokens}-token output cap. "
                       f"Thought tokens count toward that cap; use low thinking and a sufficient cap, then explicitly rerun the short probe.{thought_detail}")
        else:
            message = (f"Gemini stopped with {finish_reason} at the {max_output_tokens}-token output cap before completing the StoryPlan. "
                       f"Thinking tokens share the cap; the plan was rejected and no shot jobs were queued.{thought_detail}")
        return GeminiPlanningError("token_exhaustion", message, retryable_by_user=True, diagnostics=metadata)
    modalities = set(metadata.get("output_modalities", []))
    if isinstance(text, str) and text.strip():
        return None
    if "TEXT" not in modalities and modalities & {"AUDIO", "IMAGE", "VIDEO"}:
        metadata["category"] = "unsupported_response_modality"
        output_kinds = ", ".join(sorted(modalities)).lower()
        message = (f"Gemini returned HTTP 200 with {output_kinds.upper()} output and no text. "
                   f"This is a successful {output_kinds} response, not a text failure; this workflow needs text output for story planning.")
        return GeminiPlanningError("non_text_response", message, retryable_by_user=True, diagnostics=metadata)
    if finish_reason == "STOP":
        metadata["category"] = "empty_text_response"
        message = "Gemini stopped normally (STOP) but returned no visible text. No safety block or non-text output was reported; retry the probe only if you choose."
    else:
        metadata["category"] = "missing_text_output"
        message = "Gemini returned HTTP 200 but no usable text, finish reason, safety block, or output modality was reported. The response is inconclusive; retry explicitly if desired."
    return GeminiPlanningError("empty_response", message, retryable_by_user=True, diagnostics=metadata)


class GeminiStoryPlanner(StoryPlanner):
    provider = "gemini"

    def __init__(self, api_key: str | None, model: str | None, timeout_seconds: int = 120, client_factory=None):
        self.api_key, self.model = api_key, model
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory or _client

    @staticmethod
    def available_models(client) -> list[dict[str, Any]]:
        try:
            models = client.models.list()
            result = []
            for model in models:
                name, display, actions = _model_fields(model)
                modalities = _output_modalities(model)
                text_eligible, eligibility_source = _text_output_eligibility(name, modalities)
                compatible = {action.replace("_", "").replace("-", "").casefold() for action in actions}
                if name and _FLASH_ALLOWLIST.fullmatch(name) and "generatecontent" in compatible and text_eligible:
                    result.append({"id": name, "display_name": display,
                                   "output_modalities": list(modalities) if modalities is not None else [],
                                   "text_output_source": eligibility_source})
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
            diagnostics["category"] = "transient_provider_service"
            return GeminiPlanningError("service_unavailable",
                f"Gemini returned a temporary HTTP {status} service/capacity error during {context}. This does not indicate a schema or model-access failure. No automatic retry was made; you may explicitly repeat the short endpoint test later.",
                retryable_by_user=True, diagnostics=diagnostics)
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
            diagnostics["category"] = "model_access_or_endpoint"
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
            if context == "generation endpoint probe":
                diagnostics["category"] = "request_timeout"
                return GeminiPlanningError("timeout", "The short Gemini endpoint test exceeded the configured request timeout. Its remote outcome is unknown; no automatic retry was made. You may run the probe again manually after reviewing the status.", retryable_by_user=True, diagnostics=diagnostics)
            return GeminiPlanningError("timeout", "Gemini did not finish before the configured request timeout. Its remote outcome is unknown; the run will not resend automatically.", retryable_by_user=True, diagnostics=diagnostics)
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

    def test_connection(self) -> list[dict[str, Any]]:
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
            request_config: dict[str, Any] = {"max_output_tokens": ENDPOINT_PROBE_MAX_OUTPUT_TOKENS}
            thinking_config = _thinking_config_for_model(self.model)
            if thinking_config:
                request_config["thinking_config"] = thinking_config
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=GENERATION_ENDPOINT_PROBE_PROMPT,
                    config=request_config,
                )
            except Exception as exc:
                error = self._classify_error(exc, "generation endpoint probe")
                error.diagnostics.update({"model": model_id, "api_version": api_version,
                                          "endpoint_host": host, "endpoint_path": route,
                                          "request_shape": "text_only_no_schema_no_media",
                                          "max_output_tokens": ENDPOINT_PROBE_MAX_OUTPUT_TOKENS,
                                          "thinking_level": (thinking_config or {}).get("thinking_level")})
                if error.code == "model_or_endpoint_incompatible":
                    error.args = ("The account-listed model's basic generateContent request returned 404. "
                                  "The request used the pinned SDK route shown below; schema settings were not sent. "
                                  "Check that the Gemini API is enabled for the API key's project and confirm "
                                  "the listed model is available to this key." + self._diagnostic_suffix(error.diagnostics),)
                raise error from None
            modalities = _response_output_modalities(response)
            failure = _response_failure(response, context="generation endpoint probe",
                                        max_output_tokens=ENDPOINT_PROBE_MAX_OUTPUT_TOKENS)
            if failure:
                failure.diagnostics.update({"http_status": 200, "model": model_id, "api_version": api_version,
                                            "endpoint_host": host, "endpoint_path": route,
                                            "request_shape": "text_only_no_schema_no_media",
                                            "thinking_level": (thinking_config or {}).get("thinking_level")})
                if not failure.diagnostics.get("output_modalities"):
                    failure.diagnostics["output_modalities"] = list(modalities) or ["UNKNOWN"]
                raise failure
            text = response.text
            response_metadata = _safe_response_metadata(response, max_output_tokens=ENDPOINT_PROBE_MAX_OUTPUT_TOKENS)
            return {"success": True, "http_status": 200, "model": model_id,
                    "api_version": api_version, "endpoint_host": host,
                    "endpoint_path": route, "request_shape": "text_only_no_schema_no_media",
                    "response_text": text.strip()[:240], "thinking_level": (thinking_config or {}).get("thinking_level"),
                    "response_metadata": response_metadata}
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
        output_token_limit = _output_token_limit(shot_budget)
        config: dict[str, Any] = {"response_mime_type": "application/json",
                                  "max_output_tokens": output_token_limit}
        thinking_config = _thinking_config_for_model(self.model)
        if thinking_config:
            config["thinking_config"] = thinking_config
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
            failure = _response_failure(response, context="story planning", max_output_tokens=output_token_limit)
            if failure:
                failure.diagnostics.update({"http_status": 200,
                                           "model": self.model.removeprefix("models/"),
                                           "response_mode": response_mode,
                                           "max_output_tokens": output_token_limit,
                                           "thinking_level": (thinking_config or {}).get("thinking_level")})
                raise failure
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
                 for key in ("input_tokens", "output_tokens", "thoughts_tokens", "total_tokens")}
        return PlannerResult(plan=plan, provenance={"provider": "gemini", "model": self.model,
            "api": "Gemini generateContent API JSON Schema" if response_mode == "schema" else "Gemini generateContent API JSON-only fallback",
            "response_mode": response_mode, "sdk": "google-genai", "request_count": len(usages),
            "output_token_limit": _output_token_limit(shot_budget),
            "usage": usage, "cost_estimate": None,
            "billing_note": "Gemini billing/free-tier data policy varies by account and tier; review current Google AI Studio pricing.",
            "privacy": "Only story prompt, project style text, preset/budget/capability metadata, and opaque selected-reference IDs/categories were sent. No files or filenames were uploaded."})
