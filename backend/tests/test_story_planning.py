import json

import pytest
from pydantic import ValidationError

from app.story_planning import LocalStoryPlanner, PlannerOutputError, PlannerUnavailable, StoryPlan


def story_plan(**changes):
    data = {
        "title": "The Lighthouse Keeper",
        "logline": "A keeper guides a lost boat home before the storm reaches the harbor.",
        "visual_style": "Handmade miniature sets with tactile painted surfaces and soft dusk lighting.",
        "audio_requested": False,
        "video_continuation_requested": False,
        "ic_lora_requested": False,
        "characters": [{"id": "keeper", "name": "Mira", "identity": "An elderly lighthouse keeper with a silver braid and yellow oilskin coat.",
                        "distinguishing_traits": ["silver braid", "yellow oilskin coat"], "reference_ids": []},
                       {"id": "sailor", "name": "Noah", "identity": "A young sailor with a dark wool cap and a red scarf.",
                        "distinguishing_traits": ["dark wool cap", "red scarf"], "reference_ids": []}],
        "locations": [{"id": "harbor", "name": "Foggy harbor", "description": "A small stone harbor below the lighthouse, wrapped in coastal fog.",
                       "scale": "Human-sized docks and one distant lighthouse", "continuity_requirements": ["Keep the lighthouse on the eastern headland"]}],
        "scenes": [{"id": "storm-arrives", "sequence": 0, "title": "The storm arrives",
                    "story_beat": "Mira notices a boat lost as the storm reaches the harbor.", "location_id": "harbor",
                    "characters_present": ["keeper", "sailor"], "visual_description": "A boat appears through fog beneath the lighthouse beam.",
                    "framing": "Wide view from the lighthouse hill", "character_layout": "Keeper on the hill; sailor aboard one boat.",
                    "expected_character_count": 2, "continuity_requirements": ["One boat only", "Beam points toward the harbor mouth"],
                    "shots": [{"id": "beam-finds-boat", "sequence": 0, "title": "The beam finds the boat",
                               "beat": "Mira turns the light toward the lost boat.",
                               "prompt": "The lighthouse beam sweeps across the fog and reveals a small boat near the harbor mouth.",
                               "framing": "Wide establishing shot", "character_layout": "Mira is in the foreground; Noah remains on the boat in the background.",
                               "characters_present": ["keeper", "sailor"], "expected_character_count": 2,
                               "reference_ids": [], "caption": "", "transition_after": "cut"}]}],
    }
    data.update(changes)
    return StoryPlan.model_validate(data)


def test_story_plan_keeps_arbitrary_story_beats_identity_location_and_framing():
    plan = story_plan()
    assert plan.title == "The Lighthouse Keeper"
    assert [scene.sequence for scene in plan.scenes] == [0]
    assert plan.ordered_shots[0][1].framing == "Wide establishing shot"
    assert plan.characters[0].distinguishing_traits == ["silver braid", "yellow oilskin coat"]
    assert plan.locations[0].scale == "Human-sized docks and one distant lighthouse"


def test_story_plan_rejects_invented_characters_and_out_of_order_shots():
    data = story_plan().model_dump()
    data["scenes"][0]["shots"][0]["characters_present"] = ["invented-creature"]
    with pytest.raises(ValidationError, match="unknown character"):
        StoryPlan.model_validate(data)
    data = story_plan().model_dump()
    data["scenes"][0]["shots"][0]["sequence"] = 3
    with pytest.raises(ValidationError, match="listed order"):
        StoryPlan.model_validate(data)
    data = story_plan().model_dump()
    data["scenes"][0]["shots"][0]["expected_character_count"] = 1
    with pytest.raises(ValidationError, match="expected character count"):
        StoryPlan.model_validate(data)


def test_story_plan_rejects_unknown_or_noncharacter_reference_ids():
    data = story_plan().model_dump()
    data["characters"][0]["reference_ids"] = ["not-linked"]
    with pytest.raises(PlannerOutputError, match="not linked"):
        StoryPlan.model_validate(data).validate_project_scope(set(), {})
    data = story_plan().model_dump()
    data["characters"][0]["reference_ids"] = ["style-image"]
    with pytest.raises(PlannerOutputError, match="non-character"):
        StoryPlan.model_validate(data).validate_project_scope({"style-image"}, {"style-image": "style"})


def test_unconfigured_planner_is_an_explicit_blocker():
    planner = LocalStoryPlanner()
    assert planner.availability()["status"] == "not_configured"
    with pytest.raises(PlannerUnavailable, match="will not invent a plan"):
        planner.plan({"prompt": "A lighthouse story"})


def test_planner_command_must_be_a_json_argument_array_without_shell():
    planner = LocalStoryPlanner("sh -c 'echo fake'")
    with pytest.raises(PlannerUnavailable, match="JSON array"):
        planner.command()


def test_local_planner_cli_contract_validates_response(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(story_plan().model_dump()), encoding="utf-8")
    script = tmp_path / "planner-fixture.py"
    script.write_text("#!/usr/bin/env python3\nimport json,os,sys\nassert os.environ['HF_HUB_OFFLINE']=='1'\nassert os.environ['TRANSFORMERS_OFFLINE']=='1'\nrequest=json.load(sys.stdin)\nprint(open(" + repr(str(plan_path)) + ").read())\n", encoding="utf-8")
    script.chmod(0o755)
    planner = LocalStoryPlanner(json.dumps([str(script)]), timeout_seconds=10)
    plan = planner.plan({"prompt": "A lighthouse story", "allowed_reference_ids": [], "project_reference_catalog": []})
    assert planner.availability()["available"] is True
    assert plan.title == "The Lighthouse Keeper"


def test_local_planner_rejects_malformed_stdout(tmp_path):
    script = tmp_path / "bad-planner.py"
    script.write_text("#!/usr/bin/env python3\nprint('{not json}')\n", encoding="utf-8")
    script.chmod(0o755)
    planner = LocalStoryPlanner(json.dumps([str(script)]), timeout_seconds=10)
    with pytest.raises(PlannerOutputError, match="invalid structured data"):
        planner.plan({"prompt": "A story", "allowed_reference_ids": [], "project_reference_catalog": []})
