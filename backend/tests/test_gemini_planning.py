from types import SimpleNamespace

import pytest

from app.gemini_planning import GeminiPlanningError, GeminiStoryPlanner, gemini_story_schema
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


class FakeInteractions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeModels:
    def list(self):
        return [
            {"name": "models/gemini-2.5-flash", "display_name": "Gemini Flash"},
            {"name": "models/gemini-pro", "display_name": "Gemini Pro"},
            {"name": "models/gemini-3.8-flash", "display_name": "Gemini 3.8 Flash"},
        ]


class FakeClient:
    def __init__(self, responses):
        self.interactions = FakeInteractions(responses)
        self.models = FakeModels()


class FakeAPIError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"private response body {code}")


def response(value, usage=None, status="completed"):
    return SimpleNamespace(output_text=value, usage=usage or {"total_input_tokens": 25, "total_output_tokens": 300, "total_tokens": 325}, status=status)


def fixture_request(refs=None, shot_budget=5):
    return {"prompt": "A lighthouse keeper guides a lost boat home before a storm.",
            "project_style_bible": {"material": "Tactile felt and painted wood"},
            "project_aspect_ratio": "9:16", "project_frame_rate": 24,
            "project_reference_catalog": ([{"id": "opaque-ref-1", "category": "character"}] if refs else []),
            "allowed_reference_ids": (["opaque-ref-1"] if refs else []), "shot_budget": shot_budget,
            "preset": "Short", "model_capabilities": {"audio_generation": False, "video_continuation": False}}


def make_planner(client, *, model="gemini-3.8-flash", sleeper=lambda _delay: None):
    return GeminiStoryPlanner("not-a-real-test-secret", model, client_factory=lambda _key, _timeout: client, sleeper=sleeper)


def test_account_model_selection_is_limited_to_flash_models_returned_by_account():
    client = FakeClient([])
    models = GeminiStoryPlanner("test-key", None, client_factory=lambda *_: client).test_connection()
    assert [item["id"] for item in models] == ["gemini-2.5-flash", "gemini-3.8-flash"]


def test_structured_plan_uses_supported_schema_and_always_validates_with_storyplan():
    client = FakeClient([response(StoryPlan.model_validate(valid_plan()).model_dump_json())])
    result = make_planner(client).plan(fixture_request())
    call = client.interactions.calls[0]
    schema = call["response_format"]["schema"]
    assert call["model"] == "gemini-3.8-flash"
    assert call["generation_config"]["max_output_tokens"] == 8192
    assert call["store"] is False
    assert "$defs" not in str(schema) and "minItems" in str(schema)
    assert result.plan.title == "The Lighthouse Keeper"
    assert result.provenance["provider"] == "gemini"
    assert result.provenance["usage"]["total_tokens"] == 325
    assert result.provenance["cost_estimate"] is None


def test_opaque_reference_metadata_only_and_scope_rejection_repair():
    request = fixture_request(refs=True)
    request["project_reference_catalog"][0].update({"name": "private filename", "path": "/private/path/image.png", "notes": "private notes"})
    invalid = valid_plan(["invented-reference"])
    good = valid_plan(["opaque-ref-1"])
    client = FakeClient([response(StoryPlan.model_validate(invalid).model_dump_json()),
                         response(StoryPlan.model_validate(good).model_dump_json())])
    result = make_planner(client).plan(request)
    assert result.plan.ordered_shots[0][1].reference_ids == ["opaque-ref-1"]
    assert len(client.interactions.calls) == 2  # exactly one bounded schema/scope repair
    assert "/private/path" not in client.interactions.calls[0]["input"]
    assert "private filename" not in client.interactions.calls[0]["input"]
    assert "private notes" not in client.interactions.calls[0]["input"]


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
    assert len(client.interactions.calls) == 2


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

    client = FakeClient([response(""), response(StoryPlan.model_validate(valid_plan()).model_dump_json())])
    assert make_planner(client).plan(fixture_request()).plan.title == "The Lighthouse Keeper"
    assert len(client.interactions.calls) == 2


def test_only_bounded_503_retry_and_timeout_does_not_automatically_resend():
    client = FakeClient([FakeAPIError(503), response(StoryPlan.model_validate(valid_plan()).model_dump_json())])
    delays = []
    result = make_planner(client, sleeper=delays.append).plan(fixture_request())
    assert result.plan.title == "The Lighthouse Keeper"
    assert len(client.interactions.calls) == 2
    assert delays == [0.5]

    client = FakeClient([TimeoutError("key and prompt must not be included")])
    with pytest.raises(GeminiPlanningError) as timeout:
        make_planner(client).plan(fixture_request())
    assert timeout.value.code == "timeout"
    assert "key and prompt" not in str(timeout.value)
    assert len(client.interactions.calls) == 1


def test_schema_has_no_unsupported_pydantic_reference_definitions():
    schema = gemini_story_schema()
    assert schema["type"] == "object"
    assert "$ref" not in str(schema)
    assert "minLength" not in str(schema)
    assert "maxLength" not in str(schema)
