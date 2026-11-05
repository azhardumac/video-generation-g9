"""Opening frames for film shots: the approved character sheets placed on an empty plate of the shot's location.

LTX given only text could not keep two clay animals apart in one shot (duplicates and hybrids at every quality),
but animating from a frame that already shows each character on-model kept them separate and on-model.
"""

from __future__ import annotations

import re
from pathlib import Path

# Fraction of the frame width one character takes, by framing class.
CHARACTER_WIDTH = {"extreme close-up": 0.8, "close-up": 0.7, "medium close-up": 0.6, "medium": 0.45,
                   "medium wide": 0.38, "wide": 0.28}
_SIDES = {"left": 0.27, "right": 0.73, "center": 0.5, "centre": 0.5, "middle": 0.5}
_SIDE = re.compile(r"\b(left|right|center|centre|middle)\b")
# "facing right" says where a character looks, not where it stands.
_GAZE = re.compile(r"\b(?:facing|faces|face|looking|looks|turned|turns|toward|towards)\s+(?:to\s+)?(?:the\s+)?(?:left|right)\b")
_RIDE = (r"\b(?:on top of|atop|riding on|riding|rides on|rides)\s+(?:the\s+)?{carrier}\b",
         r"\bon\s+(?:top of\s+)?(?:the\s+)?{carrier}'s\s+(?:back|head|shoulders)\b")


def _clauses(text: str) -> list[str]:
    return [part for part in re.split(r"[;.]", _GAZE.sub(" ", text.casefold())) if part.strip()]


def _mentions(clause: str, name: str) -> list[int]:
    return [match.start() for match in re.finditer(r"\b" + re.escape(name.casefold()) + r"\b", clause)]


def _side(layout: str, name: str, names: list[str]) -> float | None:
    for clause in _clauses(layout):
        starts = _mentions(clause, name)
        if not starts:
            continue
        others = sorted(i for other in names if other != name for i in _mentions(clause, other))
        if not others:
            match = _SIDE.search(clause)
        else:
            # Several characters in one clause: the side named right after this one's name is its own.
            end = next((i for i in others if i > starts[0]), len(clause))
            match = _SIDE.search(clause, starts[0], end)
        if match:
            return _SIDES[match.group(1)]
    return None


def _carrier(layout: str, name: str, names: list[str]) -> int | None:
    for clause in _clauses(layout):
        for start in _mentions(clause, name):
            for index, other in enumerate(names):
                carrier = re.escape(other.casefold())
                if other != name and any(re.search(pattern.format(carrier=carrier), clause[start:]) for pattern in _RIDE):
                    return index
    return None


def layout_placements(layout: str, names: list[str], framing_class: str) -> list[dict]:
    """Where each named character goes in the opening frame: centre x and width as fractions of the frame.

    A character the layout puts on another's back ("the scorpion sits on top of the frog's back") rides it.
    """
    count = len(names)
    riders = {index: carrier for index, name in enumerate(names)
              if (carrier := _carrier(layout, name, names)) is not None and _carrier(layout, names[carrier], names) is None}
    standing = [index for index in range(count) if index not in riders]
    width = min(CHARACTER_WIDTH.get(framing_class, 0.45), 0.92 / max(1, len(standing)))
    sides = {index: _side(layout, names[index], names) for index in standing}
    if len(set(sides.values())) != len(standing) or None in sides.values():
        sides = {index: (slot + 0.5) / len(standing) for slot, index in enumerate(standing)}
    placements = []
    for index in range(count):
        if index in riders:
            placements.append({"x": sides[riders[index]], "width": round(width * 0.6, 3), "rider_of": riders[index]})
        else:
            placements.append({"x": round(sides[index], 3), "width": round(width, 3), "rider_of": None})
    return placements


def _cutout(path: str):
    """The character on a near-white sheet as RGBA; None when the sheet has no plain light background."""
    from PIL import Image, ImageFilter

    image = Image.open(path).convert("RGB")
    image.thumbnail((1024, 1024))
    width, height = image.size
    border = [image.getpixel((x, y)) for x in range(0, width, max(1, width // 40)) for y in (0, height - 1)]
    border += [image.getpixel((x, y)) for y in range(0, height, max(1, height // 40)) for x in (0, width - 1)]
    if sum(min(pixel) > 200 for pixel in border) < 0.8 * len(border):
        return None
    dark = image.convert("L").point(lambda v: 255 if v < 150 else 0)
    coloured = image.convert("HSV").getchannel("S").point(lambda v: 255 if v > 35 else 0)
    mask = Image.composite(Image.new("L", image.size, 255), dark, coloured)
    mask = mask.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.GaussianBlur(2))
    box = mask.getbbox()
    if not box or (box[2] - box[0]) * (box[3] - box[1]) > 0.95 * width * height:
        return None
    cut = image.crop(box)
    cut.putalpha(mask.crop(box))
    return cut


def _match_light(cut, plate):
    """Tint a studio-lit sheet halfway toward the plate's colour cast and no brighter than the plate."""
    from PIL import Image, ImageStat

    means = ImageStat.Stat(plate).mean[:3]
    luma = sum(means) / 3 or 1
    gains = [0.5 + 0.5 * mean / luma for mean in means]
    level = min(1.0, max(0.5, luma / 140))
    bands = [band.point(lambda v, g=gain * level: min(255, int(v * g))) for band, gain in zip(cut.split()[:3], gains)]
    return Image.merge("RGBA", (*bands, cut.getchannel("A")))


def compose_opening_frame(plate_path: str, characters: list[dict], output: str) -> bool:
    """Paste each character's sheet onto the plate per its placement; False when any sheet can't be cut out."""
    from PIL import Image

    plate = Image.open(plate_path).convert("RGB")
    frame_width, frame_height = plate.size
    cuts = [_cutout(item["path"]) for item in characters]
    if any(cut is None for cut in cuts):
        return False
    boxes: dict[int, tuple[int, int, int, int]] = {}
    order = sorted(range(len(characters)), key=lambda i: characters[i].get("rider_of") is not None)
    for index in order:
        item, cut = characters[index], _match_light(cuts[index], plate)
        width = int(frame_width * item["width"])
        height = int(cut.height * width / cut.width)
        if height > 0.85 * frame_height:
            height = int(0.85 * frame_height)
            width = int(cut.width * height / cut.height)
        cut = cut.resize((max(1, width), max(1, height)))
        left = int(frame_width * item["x"] - width / 2)
        left = min(max(left, 0), max(0, frame_width - width))
        carrier = boxes.get(item.get("rider_of")) if item.get("rider_of") is not None else None
        # Riders sit on their carrier's back, a third of the way down its body.
        bottom = carrier[1] + (carrier[3] - carrier[1]) // 3 if carrier else int(frame_height * 0.94)
        top = max(0, bottom - height)
        plate.paste(cut, (left, top), cut)
        boxes[index] = (left, top, left + width, top + height)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    plate.save(output)
    return True
