import json

from app.config import Settings
from app.integrations import detect_integrations
from app.projects import scan_project


def test_integration_detection_reads_install_paths_without_downloading(tmp_path):
    ltx = tmp_path / "ltx"
    runner = ltx / ".venv/bin/ltx-2-mlx"
    cache = tmp_path / "hf"
    weights = cache / "models--dgrauet--ltx-2.3-mlx-q8/snapshots/local/model.safetensors"
    remotion = tmp_path / "remotion"
    remotion_cli = remotion / "node_modules/.bin/remotion"
    story = tmp_path / "story"
    for folder in (runner.parent, weights.parent, remotion_cli.parent, story):
        folder.mkdir(parents=True, exist_ok=True)
    runner.write_text("local runner")
    weights.write_bytes(b"test fixture only")
    remotion_cli.write_text("local cli")
    (remotion / "package.json").write_text(json.dumps({"dependencies": {"@remotion/cli": "4.test"}}))
    settings = Settings(_env_file=None, ltx_root=str(ltx), ltx_executable=str(runner), hf_cache=str(cache),
                        remotion_studio=str(remotion), remotion_executable=str(remotion_cli), story_project=str(story),
                        allowed_path_roots=str(tmp_path))

    status = detect_integrations(settings)
    assert status["ltx"]["ready"] is True
    assert status["ltx"]["weights_cached"] is True
    assert status["remotion"]["version"] == "4.test"
    assert status["story_project"]["available"] is True


def test_project_scanner_indexes_images_and_videos_without_copying(tmp_path):
    refs = tmp_path / "references"
    scenes = refs / "scenes"
    characters = refs / "characters"
    shots = tmp_path / "shots" / "opening"
    for folder in (scenes, characters, shots):
        folder.mkdir(parents=True)
    (scenes / "riverbank-master.png").write_bytes(b"image")
    (characters / "frog-approved.png").write_bytes(b"image")
    (shots / "shot-01.mp4").write_bytes(b"video")

    assets = scan_project(tmp_path)
    categories = {asset["relative_path"]: asset["category"] for asset in assets}
    assert categories["references/scenes/riverbank-master.png"] == "scene_reference"
    assert categories["references/characters/frog-approved.png"] == "character_reference"
    assert categories["shots/opening/shot-01.mp4"] == "video"
    assert len(assets) == 3
