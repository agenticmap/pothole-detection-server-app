"""The box transform that has to move with a rotated frame.

Pure maths, no database, no files. It is tested on its own because it is applied
to real ground truth exactly once, in a script, and a sign error would silently
relocate a human's box onto a different part of the road -- which is indistinguishable
from a badly-drawn box after the fact.
"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fix_frame_orientation",
    Path(__file__).resolve().parent.parent / "scripts" / "fix_frame_orientation.py",
)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

rotate_box_cw = _MOD.rotate_box_cw
LANDSCAPE = _MOD.LANDSCAPE
PORTRAIT = _MOD.PORTRAIT

# The CHECK on frame_box (migrations/013). The slack exists so a box drawn flush to
# the edge is not rejected by floating-point drift.
EDGE = 1.0001


def test_four_rotations_return_the_original():
    """The only property that proves the transform is a true rotation."""
    box = (0.2, 0.3, 0.25, 0.15)
    out = box
    for _ in range(4):
        out = rotate_box_cw(*out)
    assert out == pytest.approx(box)


def test_width_and_height_swap():
    """A 90-degree turn takes a wide box to a tall one, like the frame itself."""
    _, _, w, h = rotate_box_cw(0.1, 0.2, 0.4, 0.1)
    assert (w, h) == pytest.approx((0.1, 0.4))


def test_the_top_left_corner_becomes_the_top_right():
    """Clockwise: the left edge becomes the top, so x=0 lands at the far right.

    A box hugging the top-left of a sideways frame is, after turning the image
    clockwise, hugging the TOP-RIGHT. Getting this backwards is the sign error the
    round-trip test alone would not catch -- an anticlockwise transform also
    round-trips in four steps.
    """
    x, y, w, h = rotate_box_cw(0.0, 0.0, 0.2, 0.1)
    assert x == pytest.approx(0.9)   # 1 - 0 - 0.1
    assert y == pytest.approx(0.0)
    assert (x + w) == pytest.approx(1.0)  # flush to the right edge


def test_a_full_frame_box_stays_a_full_frame_box():
    assert rotate_box_cw(0.0, 0.0, 1.0, 1.0) == pytest.approx((0.0, 0.0, 1.0, 1.0))


@pytest.mark.parametrize(
    "box",
    [
        (0.0, 0.0, 1.0, 1.0),      # whole frame
        (0.0, 0.0, 0.001, 0.001),  # tiny, at the origin
        (0.999, 0.999, 0.001, 0.001),  # tiny, at the far corner
        (0.5, 0.0, 0.5, 1.0),      # flush right, full height
        (0.0, 0.5, 1.0, 0.5),      # flush bottom, full width
    ],
)
def test_every_rotation_satisfies_the_database_constraint(box):
    """frame_box refuses a box that runs past the edge, so the transform must not."""
    out = box
    for _ in range(4):
        out = rotate_box_cw(*out)
        x, y, w, h = out
        assert 0.0 <= x <= EDGE and 0.0 <= y <= EDGE
        assert x + w <= EDGE, f"{out} breaks x + w <= {EDGE}"
        assert y + h <= EDGE, f"{out} breaks y + h <= {EDGE}"


def test_the_shape_selector_is_the_two_orientations_and_nothing_else():
    """The script picks frames by pixel shape, since no other orientation record exists.

    Pinned because a typo here would either rotate the whole corpus or none of it.
    """
    assert LANDSCAPE == (640, 480)
    assert PORTRAIT == (480, 640)
    assert LANDSCAPE == PORTRAIT[::-1]
