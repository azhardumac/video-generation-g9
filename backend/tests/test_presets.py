import pytest

from app.presets import BUILTIN_PRESETS, GenerationSettings, Preset


@pytest.mark.parametrize("name,frames,duration", [
    ("Short", 49, 2.04),
    ("Standard", 97, 4.04),
    ("Extended", 145, 6.04),
    ("Experimental", 193, 8.04),
])
def test_benchmark_presets_match_spec(name, frames, duration):
    preset = next(item for item in BUILTIN_PRESETS if item.name == name)
    assert preset.frames == frames
    assert preset.fps == 24
    assert preset.duration == duration


def test_experimental_preset_warns_about_visual_drift():
    preset = next(item for item in BUILTIN_PRESETS if item.name == "Experimental")
    assert preset.visual_drift_warning is True


def test_custom_preset_requires_ltx_frame_stride():
    with pytest.raises(ValueError):
        Preset(name="Invalid", frames=50, fps=24)


def test_generation_resolution_is_divisible_by_eight():
    with pytest.raises(ValueError):
        GenerationSettings(width=450)
