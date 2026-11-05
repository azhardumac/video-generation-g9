from PIL import Image

from app.keyframes import compose_opening_frame, layout_placements


def _sheet(path, colour, background=(250, 250, 250)):
    image = Image.new("RGB", (200, 300), background)
    image.paste(colour, (50, 60, 150, 260))
    image.save(path)
    return str(path)


def test_layout_sides_follow_the_named_character_not_its_gaze():
    names = ["Frog", "Scorpion"]
    assert [p["x"] for p in layout_placements("Frog on the left facing right, scorpion on the right", names, "medium")] == [0.27, 0.73]
    assert [p["x"] for p in layout_placements("On the left, the scorpion; on the right, the frog.", names, "medium")] == [0.73, 0.27]
    assert [p["x"] for p in layout_placements("Keeper on the hill; sailor aboard one boat.", ["Mira", "Noah"], "wide")] == [0.25, 0.75]
    assert layout_placements("The frog faces left", ["Frog"], "close-up") == [{"x": 0.5, "width": 0.7, "rider_of": None}]


def test_a_character_on_anothers_back_rides_it():
    riding = layout_placements("The scorpion sits on top of the frog's back as it swims", ["Frog", "Scorpion"], "medium")
    assert riding[0]["rider_of"] is None and riding[1]["rider_of"] == 0
    assert riding[1]["x"] == riding[0]["x"] and riding[1]["width"] < riding[0]["width"]
    side_by_side = layout_placements("The scorpion stands on the bank beside the frog", ["Frog", "Scorpion"], "medium")
    assert all(p["rider_of"] is None for p in side_by_side)


def test_sheets_are_cut_from_their_white_background_and_placed(tmp_path):
    Image.new("RGB", (576, 1024), (200, 150, 90)).save(tmp_path / "plate.png")
    frog, scorpion = _sheet(tmp_path / "frog.png", (60, 160, 40)), _sheet(tmp_path / "scorpion.png", (170, 60, 40))
    out = tmp_path / "opening.png"
    assert compose_opening_frame(str(tmp_path / "plate.png"), [{"path": frog, "x": 0.27, "width": 0.45, "rider_of": None},
                                                               {"path": scorpion, "x": 0.73, "width": 0.45, "rider_of": None}], str(out))
    frame = Image.open(out).convert("RGB")
    assert frame.size == (576, 1024)
    left, right = frame.getpixel((155, 800)), frame.getpixel((420, 800))
    assert left[1] > left[0] and right[0] > right[1]
    # The sheet's white backdrop is not pasted: the plate shows around each character.
    assert frame.getpixel((155, 300)) == (200, 150, 90)


def test_sheet_without_a_plain_background_is_refused(tmp_path):
    Image.new("RGB", (576, 1024), (200, 150, 90)).save(tmp_path / "plate.png")
    photo = _sheet(tmp_path / "photo.png", (60, 160, 40), background=(30, 90, 140))
    assert not compose_opening_frame(str(tmp_path / "plate.png"), [{"path": photo, "x": 0.5, "width": 0.45, "rider_of": None}],
                                     str(tmp_path / "opening.png"))
