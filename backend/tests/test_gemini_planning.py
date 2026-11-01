from types import SimpleNamespace

import pytest

from app.gemini_planning import (MAX_OUTPUT_TOKENS, GeminiPlanningError, GeminiStoryPlanner,
                                 _output_token_limit, gemini_story_schema)
from app.story_planning import PlannerOutputError, StoryPlan


def valid_plan(reference_ids=None):
    reference_ids = reference_ids or []
    return {
        "title": "The Lighthouse Keeper",
        "logline": "A lighthouse keeper guides a lost boat home before the storm reaches the harbor.",
        "visual_style": "Handmade miniature sets with tactile painted surfaces and soft dusk lighting.",
        "audio_requested": False,
        "video_continuation_requested": False,
        "ic_lora_requested": False,
        "characters": [{"id": "keeper", "name": "Mira", "identity": "An elderly lighthouse keeper with a silver braid and yellow oilskin coat.",
                        "distinguishing_traits": ["silver braid", "yellow oilskin coat"], "reference_ids": reference_ids}],
        "locations": [{"id": "harbor", "name": "Foggy harbor", "description": "A small stone harbor below the lighthouse wrapped in coastal fog.",
                       "scale": "Human-sized docks and one distant lighthouse", "continuity_requirements": ["Keep lighthouse on eastern headland"]}],
        "scenes": [{"id": "storm-arrives", "sequence": 0, "title": "The storm arrives",
                    "story_beat": "Mira notices a boat lost as the storm reaches the harbor.", "location_id": "harbor",
                    "characters_present": ["keeper"], "visual_description": "A boat appears through fog beneath the lighthouse beam.",
                    "framing": "Wide view from the lighthouse hill", "character_layout": "Mira stands on the hill above the harbor.",
                    "continuity_requirements": ["One boat only", "Beam points toward harbor mouth"], "expected_character_count": 1,
                    "shots": [{"id": "beam-finds-boat", "sequence": 0, "title": "The beam finds the boat",
                               "beat": "Mira turns the light toward the lost boat.",
                               "prompt": "The lighthouse beam sweeps across the fog and reveals a small boat near the harbor mouth.",
                               "framing": "Wide establishing shot", "character_layout": "Mira is foreground left, harbor fills background.",
                               "characters_present": ["keeper"], "expected_character_count": 1,
                               "reference_ids": reference_ids, "caption": "", "transition_after": "cut"}]}],
    }


class FakeGenerateContentModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


    def list(self):
        return [
            {"name": "models/gemini-2.5-flash", "display_name": "Gemini Flash", "supportedActions": ["generateContent"]},
            {"name": "models/gemini-pro", "display_name": "Gemini Pro", "supportedActions": ["generateContent"]},
            {"name": "models/gemini-3.8-flash", "display_name": "Gemini 3.8 Flash", "supportedActions": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-image", "display_name": "Flash Image", "supportedActions": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-preview-interactions", "display_name": "Interactions only", "supportedActions": ["interactions"]},
        ]


class FakeClient:
    def __init__(self, responses):
        self.models = FakeGenerateContentModels(responses)


class FakeAPIError(Exception):
    def __init__(self, code, details=None):
        self.code = code
        self.details = details
        super().__init__(f"private response body {code}")


def response(value, usage=None, finish_reason="STOP"):
    return SimpleNamespace(text=value, usage_metadata=usage or {"prompt_token_count": 25, "candidates_token_count": 300, "total_token_count": 325},
                           candidates=[SimpleNamespace(finish_reason=finish_reason)])


def fixture_request(refs=None, shot_budget=5):
    return {"prompt": "A lighthouse keeper guides a lost boat home before a storm.",
            "project_style_bible": {"material": "Tactile felt and painted wood"},
            "project_aspect_ratio": "9:16", "project_frame_rate": 24,
            "project_reference_catalog": ([{"id": "opaque-ref-1", "category": "character"}] if refs else []),
            "allowed_reference_ids": (["opaque-ref-1"] if refs else []), "shot_budget": shot_budget,
            "preset": "Short", "model_capabilities": {"audio_generation": False, "video_continuation": False}}


def make_planner(client, *, model="gemini-3.8-flash"):
    return GeminiStoryPlanner("not-a-real-test-secret", model, client_factory=lambda _key, _timeout: client)


def test_account_model_selection_is_limited_to_flash_models_returned_by_account():
    client = FakeClient([])
    models = GeminiStoryPlanner("test-key", None, client_factory=lambda *_: client).test_connection()
    assert [item["id"] for item in models] == ["gemini-2.5-flash", "gemini-3.8-flash"]


def test_structured_plan_uses_supported_schema_and_always_validates_with_storyplan():
    client = FakeClient([response(StoryPlan.model_validate(valid_plan()).model_dump_json())])
    result = make_planner(client).plan(fixture_request(shot_budget=1))
    call = client.models.calls[0]
    schema = call["config"]["response_json_schema"]
    assert call["model"] == "gemini-3.8-flash"
    assert "response_schema" not in call["config"]
    assert call["config"]["max_output_tokens"] == 3712
    assert call["config"]["response_mime_type"] == "application/json"
    assert call["contents"].startswith("Create a film story plan")
    assert schema["properties"]["scenes"]["maxItems"] == 1
    assert schema["properties"]["scenes"]["items"]["properties"]["shots"]["maxItems"] == 1
    assert schema["properties"]["locations"]["maxItems"] == 1
    assert "$defs" not in str(schema) and "minItems" in str(schema)
    assert result.plan.title == "The Lighthouse Keeper"
    assert result.provenance["provider"] == "gemini"
    assert result.provenance["usage"]["total_tokens"] == 325
    assert result.provenance["api"] == "Gemini generateContent API JSON Schema"
    assert result.provenance["output_token_limit"] == 3712
    assert result.provenance["cost_estimate"] is None


def test_pinned_google_genai_sdk_serializes_schema_and_json_only_configs():
    from importlib.metadata import version
    from google.genai import types

    assert version("google-genai") == "2.33.0"
    schema_config = types.GenerateContentConfig.model_validate({
        "response_mime_type": "application/json", "response_json_schema": gemini_story_schema(1),
        "max_output_tokens": _output_token_limit(1),
    }).model_dump(by_alias=True, exclude_none=True)
    json_config = types.GenerateContentConfig.model_validate({
        "response_mime_type": "application/json", "max_output_tokens": _output_token_limit(1),
    }).model_dump(by_alias=True, exclude_none=True)
    assert schema_config["responseMimeType"] == "application/json"
    assert schema_config["responseJsonSchema"]["properties"]["scenes"]["maxItems"] == 1
    assert json_config == {"responseMimeType": "application/json", "maxOutputTokens": 3712}
    assert "responseJsonSchema" not in json_config


def test_opaque_reference_metadata_only_and_scope_rejection_repair():
    request = fixture_request(refs=True)
    request["project_reference_catalog"][0].update({"name": "private filename", "path": "/private/path/image.png", "notes": "private notes"})
    invalid = valid_plan(["invented-reference"])
    good = valid_plan(["opaque-ref-1"])
    client = FakeClient([response(StoryPlan.model_validate(invalid).model_dump_json()),
                         response(StoryPlan.model_validate(good).model_dump_json())])
    result = make_planner(client).plan(request)
    assert result.plan.ordered_shots[0][1].reference_ids == ["opaque-ref-1"]
    assert len(client.models.calls) == 2  # exactly one bounded schema/scope repair
    assert "/private/path" not in client.models.calls[0]["contents"]
    assert "private filename" not in client.models.calls[0]["contents"]
    assert "private notes" not in client.models.calls[0]["contents"]


def test_shot_budget_is_checked_and_invalid_second_output_is_a_blocker():
    two_shots = valid_plan()
    second = dict(two_shots["scenes"][0]["shots"][0]) | {"id": "second-shot", "sequence": 1}
    two_shots["scenes"][0]["shots"].append(second)
    # The internal StoryPlan schema accepts two shots, but the run's cap is one.
    output = StoryPlan.model_validate(two_shots).model_dump_json()
    client = FakeClient([response(output), response(output)])
    with pytest.raises(GeminiPlanningError, match="remained invalid") as error:
        make_planner(client).plan(fixture_request(shot_budget=1))
    assert error.value.code == "invalid_output"
    assert len(client.models.calls) == 2


def test_unapproved_reference_is_rejected_after_bounded_repair():
    request = fixture_request(refs=True)
    invalid = StoryPlan.model_validate(valid_plan(["invented-reference"])).model_dump_json()
    client = FakeClient([response(invalid), response(invalid)])
    with pytest.raises(GeminiPlanningError) as error:
        make_planner(client).plan(request)
    assert error.value.code == "invalid_output"
    assert "invented-reference" not in str(error.value)
    assert len(client.models.calls) == 2


def test_missing_key_invalid_model_auth_quota_and_empty_output_are_classified_without_secret_echo():
    with pytest.raises(GeminiPlanningError) as missing:
        GeminiStoryPlanner(None, None).plan(fixture_request())
    assert missing.value.code == "missing_key"

    with pytest.raises(GeminiPlanningError) as invalid_model:
        GeminiStoryPlanner("secret-value", "gemini-pro").plan(fixture_request())
    assert invalid_model.value.code == "model_invalid"
    assert "secret-value" not in str(invalid_model.value)

    client = FakeClient([FakeAPIError(401)])
    with pytest.raises(GeminiPlanningError) as denied:
        make_planner(client).plan(fixture_request())
    assert denied.value.code == "auth_denied"
    assert "secret-value" not in str(denied.value)

    client = FakeClient([FakeAPIError(429)])
    with pytest.raises(GeminiPlanningError) as limited:
        make_planner(client).plan(fixture_request())
    assert limited.value.code == "rate_limit"

    client = FakeClient([FakeAPIError(400)])
    with pytest.raises(GeminiPlanningError) as rejected:
        make_planner(client).plan(fixture_request())
    assert rejected.value.code == "schema_rejected"
    assert "JSON Schema" in str(rejected.value)
    assert "private response body" not in str(rejected.value)
    assert len(client.models.calls) == 1

    client = FakeClient([response(""), response(StoryPlan.model_validate(valid_plan()).model_dump_json())])
    assert make_planner(client).plan(fixture_request()).plan.title == "The Lighthouse Keeper"
    assert len(client.models.calls) == 2


def test_transient_provider_error_is_not_automatically_retried():
    client = FakeClient([FakeAPIError(503), response(StoryPlan.model_validate(valid_plan()).model_dump_json())])
    with pytest.raises(GeminiPlanningError) as unavailable:
        make_planner(client).plan(fixture_request())
    assert unavailable.value.code == "service_unavailable"
    assert unavailable.value.retryable_by_user
    assert len(client.models.calls) == 1

    client = FakeClient([TimeoutError("key and prompt must not be included")])
    with pytest.raises(GeminiPlanningError) as timeout:
        make_planner(client).plan(fixture_request())
    assert timeout.value.code == "timeout"
    assert "key and prompt" not in str(timeout.value)
    assert len(client.models.calls) == 1


def test_404_identifies_model_or_endpoint_incompatibility_without_private_text():
    client = FakeClient([FakeAPIError(404)])
    with pytest.raises(GeminiPlanningError) as missing:
        make_planner(client).plan(fixture_request())
    assert missing.value.code == "model_or_endpoint_incompatible"
    assert "generateContent" in str(missing.value)
    assert "private response body" not in str(missing.value)
    assert "not-a-real-test-secret" not in str(missing.value)
    assert "A lighthouse keeper guides a lost boat" not in str(missing.value)
    assert len(client.models.calls) == 1


def test_schema_400_exposes_only_sanitized_provider_status_reason_and_field_path():
    details = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
        "message": "PRIVATE_PROMPT_AND_PROVIDER_RESPONSE_BODY",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.BadRequest", "fieldViolations": [
                {"field": "generationConfig.responseJsonSchema.properties.scenes", "description": "PRIVATE_PROMPT_AND_SCHEMA_CONTENT"}]},
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SCHEMA_VALIDATION_FAILED",
             "metadata": {"private": "PRIVATE_RESPONSE_METADATA"}},
        ]}}
    client = FakeClient([FakeAPIError(400, details)])
    with pytest.raises(GeminiPlanningError) as error:
        make_planner(client).plan(fixture_request(shot_budget=1))
    assert error.value.code == "schema_rejected"
    assert error.value.diagnostics == {"http_status": 400, "provider_status": "INVALID_ARGUMENT",
        "reason": "SCHEMA_VALIDATION_FAILED", "field_paths": ["generationConfig.responseJsonSchema.properties.scenes"],
        "response_mode": "schema", "category": "response_schema_rejected"}
    assert "generationConfig.responseJsonSchema.properties.scenes" in str(error.value)
    assert "PRIVATE_" not in str(error.value)
    assert "PRIVATE_" not in str(error.value.diagnostics)


def test_json_only_mode_omits_provider_schema_and_still_validates_storyplan_locally():
    request = fixture_request(shot_budget=1) | {"response_mode": "json_only"}
    valid = StoryPlan.model_validate(valid_plan()).model_dump_json()
    client = FakeClient([response(valid)])
    result = make_planner(client).plan(request)
    config = client.models.calls[0]["config"]
    assert config == {"response_mime_type": "application/json", "max_output_tokens": 3712}
    assert result.plan.title == "The Lighthouse Keeper"
    assert result.provenance["response_mode"] == "json_only"

    invalid = valid_plan(["not-approved"])
    client = FakeClient([response(StoryPlan.model_validate(invalid).model_dump_json()), response("still invalid")])
    with pytest.raises(GeminiPlanningError) as rejected:
        make_planner(client).plan(request | {"allowed_reference_ids": ["approved"],
            "project_reference_catalog": [{"id": "approved", "category": "character"}]})
    assert rejected.value.code == "invalid_output"
    assert all("response_json_schema" not in call["config"] for call in client.models.calls)


def test_invalid_json_uses_one_bounded_schema_repair():
    client = FakeClient([response("{\"title\":"), response(StoryPlan.model_validate(valid_plan()).model_dump_json())])
    result = make_planner(client).plan(fixture_request())
    assert result.plan.title == "The Lighthouse Keeper"
    assert len(client.models.calls) == 2
    assert "INVALID_RESPONSE_JSON" in client.models.calls[1]["contents"]


def test_schema_has_no_unsupported_pydantic_reference_definitions():
    schema = gemini_story_schema()
    assert schema["type"] == "object"
    assert "$ref" not in str(schema)
    assert "minLength" not in str(schema)
    assert "maxLength" not in str(schema)


def test_schema_and_response_limit_scale_to_authorized_shot_budget():
    one = gemini_story_schema(1)
    eight = gemini_story_schema(8)
    assert one["properties"]["scenes"]["maxItems"] == 1
    assert one["properties"]["scenes"]["items"]["properties"]["shots"]["maxItems"] == 1
    assert eight["properties"]["scenes"]["maxItems"] == 8
    assert eight["properties"]["scenes"]["items"]["properties"]["shots"]["maxItems"] == 8
    assert one["properties"]["locations"]["maxItems"] == 1
    assert _output_token_limit(1) == 3712
    assert _output_token_limit(1) < _output_token_limit(4) < _output_token_limit(8) == MAX_OUTPUT_TOKENS


def test_provider_schema_omits_unsupported_max_lengths_but_local_storyplan_keeps_them():
    too_long = valid_plan()
    too_long["title"] = "T" * 161
    with pytest.raises(ValueError):
        StoryPlan.model_validate(too_long)
