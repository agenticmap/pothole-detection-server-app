"""The Model A class set — one definition, imported by everything that depends on it.

Phase 2.7b. Three artefacts have to agree on this list *by position*, because a class
is an integer everywhere it matters: the labelling tool's palette
(`scripts/label_frames.py`), the YOLO dataset's `data.yaml names:`
(`scripts/export_labeled_frames.py`), and the decoder's `labels`
(`app/detection/onnx_v1.py`, via `DETECTION_CLASS_NAMES`). If any two disagree, every
box carries the wrong name and — far worse — `server_probability` can be sourced from
the wrong class, which fusion has no way to detect because it never sees a class at
all. So the list lives here and the others import it.

Why these, and why not others: see `docs/architecture/detection-model-strategy.md`. Briefly — they
are the objects that sit *on the road surface*, in the ROI crop, and look like each
other. `manhole`, `grate` and `patch` exist not because the platform reports them but
because they are the hard negatives that were destroying pothole recall: with one class
the model's only way to explain a manhole is "background", which it achieves by
suppressing dark-irregular-shape-on-asphalt, and a pothole is a dark irregular shape on
asphalt. Giving it somewhere else to put a manhole is the fix.

`wet/shadow` is deliberately NOT a class. It is a lighting condition, not an object;
there is nothing to draw a box around. Those frames stay background.

`crack` IS a class, with a caveat that has to be honoured at labelling time rather than
in code. Unlike the others it has no natural compact extent: a hairline longitudinal
crack is long, thin and usually diagonal, so an axis-aligned box around one is almost
entirely undamaged asphalt. That is the same argument that put lane markings in Model C
rather than here. Cracking is included anyway because much of it *does* box well —
alligator and block cracking, a spalled or crazed patch of surface, are compact regions
— and because it is real road damage a maintenance product wants to see.

**The rule is therefore: box crack REGIONS, not crack LINES.** A sliver of a box teaches
the model that "mostly undamaged asphalt" is the crack class, which is the same
suppression mechanism that cost pothole recall in v2 and v3, aimed at a new target.
`scripts/label_frames.py` warns when a crack box is drawn thinner than `_THIN_ASPECT`
below. The warning is advisory and never blocks the save: a genuinely thin region is
occasionally the right call, and only the person looking at the frame can tell.

Street furniture (signs, lights, poles) and road markings are **different models** —
Model B and Model C. They must never appear here, because anything in this list can
reach `server_probability`.
"""

from __future__ import annotations

# Position IS the class_id. Never reorder; only append.
ROAD_SURFACE_CLASSES: tuple[str, ...] = ("pothole", "manhole", "grate", "patch", "crack")

# The only class permitted to set server_probability. See onnx_v1._frame_probability.
PRIMARY_CLASS_ID = 0

# Classes whose boxes should be compact regions, and which get a thin-box warning while
# labelling. Only `crack` qualifies today: a hairline crack line boxes to a sliver of
# almost-entirely-undamaged asphalt. See the module docstring.
REGION_CLASSES: frozenset[str] = frozenset({"crack"})

# Warn below roughly 1:6. Loose on purpose -- a real alligator-cracked patch is often
# wider than it is tall, and a warning that fires constantly gets ignored, which is
# worse than no warning at all.
THIN_ASPECT_RATIO = 6.0

assert ROAD_SURFACE_CLASSES[PRIMARY_CLASS_ID] == "pothole"
assert REGION_CLASSES <= set(ROAD_SURFACE_CLASSES)


def is_thin(width: float, height: float, ratio: float = THIN_ASPECT_RATIO) -> bool:
    """True when a normalized box is more sliver than region.

    Shared by the labelling UI and the save path so the operator's warning and the
    server's log agree on what counts as thin.
    """
    short, long_ = min(width, height), max(width, height)
    return short > 0 and long_ / short > ratio
