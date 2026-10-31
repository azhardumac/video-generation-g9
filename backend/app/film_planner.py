"""Transparent, deterministic planning for the explicitly approved river fable.

No remote or local language model is configured in this application. This module
therefore accepts only the user-supplied original frog and scorpion fable and
maps its stated beats into a fixed, editable shot plan.
"""

from __future__ import annotations

import re


class UnsupportedStory(ValueError):
    pass


def validate_approved_fable(story: str) -> str:
    text = " ".join(story.split())
    lower = text.casefold()
    required = {
        "frog": r"\bfrog\b",
        "scorpion": r"\bscorpion\b",
        "river crossing": r"\briver\b|\bcross(?:ing|es|ed)?\b",
        "the sting": r"\bsting(?:s|ing)?\b|\bstung\b",
        "the scorpion's nature": r"\bnature\b",
        "the halfway point": r"\bhalfway\b|\bmidway\b",
        "the tragic ending": r"\btragic\b|\bdrown\w*\b|\bdie\w*\b|\bdeath\b",
    }
    missing = [label for label, pattern in required.items() if not re.search(pattern, lower)]
    if missing:
        raise UnsupportedStory(
            "The deterministic planner only supports the approved river-crossing fable. "
            "This story is missing: " + ", ".join(missing) + "."
        )
    def beat(pattern: str, start: int = 0) -> int:
        match = re.search(pattern, lower[start:])
        return start + match.start() if match else -1

    request = beat(r"\bscorpion\b.{0,140}\b(?:asks?|requests?)\b.{0,80}\b(?:ride|carry)\b")
    fear = beat(r"\bfrog\b.{0,120}\b(?:fears?|afraid|worries?)\b.{0,60}\bst(?:ing|ung)", max(request, 0))
    reason = beat(r"\bboth\b.{0,100}\b(?:die|drown)\b", max(fear, 0))
    agreement = beat(r"\bfrog\b.{0,100}\bagrees?\b", max(reason, 0))
    crossing = beat(r"\b(?:they|frog)\b.{0,80}\bcross(?:es|ed|ing)?\b.{0,40}\briver\b", max(agreement, 0))
    if any(index < 0 for index in (request, fear, reason, agreement, crossing)):
        raise UnsupportedStory(
            "The approved fable must keep the request, the frog's fear, the scorpion's reason "
            "that both would die, the frog's agreement, and their river crossing in that order."
        )
    halfway = beat(r"\b(?:halfway|midway)\b", crossing)
    sting_candidates = [index for index in (lower.find("stung", halfway), lower.find("sting", halfway)) if index >= 0]
    sting = min(sting_candidates, default=-1)
    question = lower.find("why", sting)
    nature = lower.find("nature", question)
    tragic_candidates = [index for index in (lower.find("tragic", nature), lower.find("drown", nature), lower.find("death", nature)) if index >= 0]
    tragic = min(tragic_candidates, default=-1)
    positions = [halfway, sting, question, nature, tragic]
    if any(index < 0 for index in positions) or positions != sorted(positions):
        raise UnsupportedStory(
            "The approved fable must keep its stated order: halfway crossing, sting, "
            "the frog's question, the nature answer, then the tragic end."
        )
    return text


def make_approved_fable_plan(*, frog: dict, scorpion: dict, style_context: str,
                             target_format: str, preset: str, frames: int, width: int, height: int,
                             fps: int, seed: int | None, output_location: str,
                             generation_profile: dict) -> tuple[list[dict], dict]:
    """Return shot records and scene overview. Each LTX shot gets one image only."""
    beats = [
        ("The Frog Searches for a Crossing", "The frog reaches the river and looks for a way across.", [
            ("At the river's edge", "The frog arrives at the riverbank and studies the water, searching for a crossing.", "The frog stands on the near bank, looking across a broad river. Moonlight reflects on moving water; reeds edge the shore.", "frog", "Wide establishing shot", "", "cut"),
            ("A crossing in view", "The frog follows the riverbank and pauses where the current passes through a clear crossing.", "The same frog examines the river current from the near bank. Keep the bank, water and far shore visually readable.", "frog", "Medium profile shot", "The frog searches for a crossing.", "cut"),
        ]),
        ("The Scorpion's Request", "The scorpion asks the frog for a ride; the frog fears being stung.", [
            ("The scorpion asks", "The scorpion approaches the frog beside the river and asks for a ride across.", "The scorpion approaches the frog at the river's edge and speaks calmly. Preserve both original character designs; the river is described for this film and is not an image reference.", "scorpion", "Two-character medium shot", "Scorpion: Please carry me across the river.", "cut"),
            ("The frog hesitates", "The frog explains the fear of being stung while they cross.", "The frog faces the scorpion at the near bank, cautious and thoughtful; the scorpion listens. Handmade clay figures, expressive eyes, consistent approved silhouettes.", "frog", "Over-the-shoulder close shot", "Frog: How do I know you won't sting me?", "cut"),
        ]),
        ("A Reason to Agree", "The scorpion reasons that a sting would kill them both; the frog agrees.", [
            ("The scorpion gives his reason", "The scorpion says that if he stung the frog, both would die in the river.", "The scorpion answers beside the river while the frog listens. Keep the action still and dialogue-led; no additional plot events.", "scorpion", "Scorpion close-up", "Scorpion: If I stung you, we would both die in the river.", "cut"),
            ("The frog agrees", "The frog accepts the request and prepares to carry the scorpion.", "The frog looks toward the water, then turns back to the scorpion with a reluctant, considered agreement.", "frog", "Frog close-up", "Frog: All right. Climb on.", "cut"),
        ]),
        ("The River Crossing", "The frog carries the scorpion as they cross the river.", [
            ("They enter the water", "The scorpion climbs onto the frog and the frog begins swimming across.", "The frog carries the scorpion on its back into the river. Show a clear departure from the near bank, handmade claymation water and gentle moonlit highlights.", "frog", "Side wide shot", "", "cut"),
            ("Halfway across", "The pair continue together through the river; they are now halfway across.", "The frog and scorpion are visibly midstream, halfway between the banks. The frog swims; the scorpion rides on its back. Preserve both character designs from the approved references.", "scorpion", "Wide tracking shot", "", "cut"),
        ]),
        ("The Sting and the Answer", "Halfway across, the scorpion stings the frog; the frog asks why; the scorpion says it is his nature.", [
            ("The scorpion stings", "At the halfway point, the scorpion stings the frog while they are in the river.", "Midstream at the halfway point, the scorpion's tail makes contact with the frog. The frog reacts in shock; keep the moment clear, restrained and non-graphic.", "scorpion", "Tight two-character shot", "", "cut"),
            ("The frog asks why", "The injured frog asks the scorpion why he stung him.", "Still in the river, the struggling frog turns toward the scorpion and asks why. Do not change their positions or add another event.", "frog", "Frog close-up", "Frog: Why did you sting me?", "cut"),
            ("The scorpion answers", "The scorpion replies that stinging the frog is in his nature.", "Still midstream, the scorpion answers the frog. Keep the tone tragic and quiet; no new characters or plot commentary.", "scorpion", "Scorpion close-up", "Scorpion: It is in my nature.", "cut"),
        ]),
        ("The Tragic End", "The frog and scorpion face the tragic end of the crossing together.", [
            ("Together in the river", "The frog and scorpion sink beneath the river as the crossing ends tragically.", "The two characters disappear beneath the river surface together. Hold on the empty moonlit water; restrained handmade claymation, no alternate ending or added aftermath.", "frog", "Wide closing shot", "", "fade"),
        ]),
    ]
    reference_by_character = {"frog": frog, "scorpion": scorpion}
    shots: list[dict] = []
    scenes: list[dict] = []
    shot_seconds = frames / fps
    position = 0
    for scene_position, (scene_title, scene_description, rows) in enumerate(beats):
        scenes.append({"title": scene_title, "position": scene_position, "visual_description": scene_description,
                       "estimated_seconds": round(len(rows) * shot_seconds, 2),
                       "transition_after": "fade" if scene_position == len(beats)-1 else "dissolve"})
        for row_index, (title, beat, visual, reference_character, framing, dialogue, transition) in enumerate(rows):
            reference = reference_by_character[reference_character]
            prompt = (
                f"Create one continuous {shot_seconds:.2f}-second handmade cinematic claymation shot. "
                "Follow only this fable beat: " + beat + " " + visual + " "
                "Use tactile sculpted clay surfaces, subtle fingerprints, miniature practical-set lighting, "
                "gentle cinematic camera movement, restrained expressive performance, and consistent character proportions. "
                "The attached single reference image conditions only the " + reference_character + "; do not treat it as a composite or as both characters. "
                "A second character may appear only according to the shot description. The text-described river environment is new for this film. "
                "Do not reuse prior video, riverbank-master.png, benchmark media, or old storyboard imagery. "
            )
            if style_context.strip():
                prompt += "Project style bible:\n" + style_context.strip() + "\n"
            prompt += "Film-specific instruction: river and water are required by this approved fable; preserve claymation material, approved character designs, and all other compatible style-bible rules."
            payload = generation_profile | {
                "prompt": prompt, "reference_image": reference["path"], "seed": seed,
                "preset": preset, "width": width, "height": height, "fps": fps,
                "output_location": output_location, "audio": False,
                "reference_conditioning": "one-image", "reference_id": reference["id"],
            }
            shots.append({
                "scene_title": scene_title, "title": title, "prompt": prompt,
                "visual_description": visual, "reference_image": reference["path"],
                "reference_ids": [reference["id"]], "characters": ["frog", "scorpion"] if "two-character" in framing or "both" in visual.lower() else [reference_character],
                "framing": framing, "preset": preset, "seed": seed, "caption": dialogue,
                "dialogue": dialogue, "width": width, "height": height, "fps": fps,
                "output_location": output_location, "estimated_seconds": round(shot_seconds, 2),
                "transition_after": scenes[-1]["transition_after"] if row_index == len(rows)-1 else transition,
                "generation_payload": payload,
            })
            position += 1
    return shots, {"scenes": scenes, "shot_count": position,
                   "estimated_seconds": round(position * shot_seconds, 2),
                   "reference_conditioning": "one-image-per-shot",
                   "reference_limit_note": "LTX receives one approved character reference per shot. Two-character shots list both characters but attach one shot-specific image; no composite was created.",
                   "planner_disclosure": "Deterministic beat mapping for the approved fable; no planning model is configured and no AI analysis was run.",
                   "available_audio": False}
