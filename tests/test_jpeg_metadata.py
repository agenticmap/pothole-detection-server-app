"""Tests for lossless JPEG metadata stripping (app/services/jpeg_metadata.py).

Road frames are photos of public streets and phones stamp GPS into EXIF, so the
archive must not hold that metadata — every stored frame is served to any
`viewer` staff account. Pure byte-level tests; no DB required.
"""

import io

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from app.services.jpeg_metadata import (
    apply_exif_orientation,
    exif_orientation,
    strip_jpeg_metadata,
)


def _jpeg_with_exif() -> bytes:
    """A real JPEG carrying camera make/model plus a GPS IFD."""
    img = Image.new("RGB", (24, 16), (120, 60, 30))
    exif = img.getexif()
    exif[0x010F] = "TestPhoneMaker"
    exif[0x0110] = "SecretModel"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (IFDRational(43), IFDRational(39), IFDRational(11))
    gps[3] = "W"
    gps[4] = (IFDRational(79), IFDRational(22), IFDRational(59))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif, quality=90)
    return buf.getvalue()


def _plain_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 16), (10, 200, 90)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_exif_and_gps_are_removed():
    original = _jpeg_with_exif()
    assert b"Exif\x00\x00" in original and b"TestPhoneMaker" in original

    clean = strip_jpeg_metadata(original)

    assert b"Exif\x00\x00" not in clean
    assert b"TestPhoneMaker" not in clean
    assert b"SecretModel" not in clean
    assert len(clean) < len(original)
    # And nothing Pillow can still read back as EXIF.
    assert dict(Image.open(io.BytesIO(clean)).getexif()) == {}


def test_stripping_is_pixel_lossless():
    """The scan data is copied byte for byte, so there is no generation loss."""
    original = _jpeg_with_exif()
    clean = strip_jpeg_metadata(original)

    before = Image.open(io.BytesIO(original)).convert("RGB")
    after = Image.open(io.BytesIO(clean)).convert("RGB")
    assert before.size == after.size
    assert list(before.getdata()) == list(after.getdata())


def test_result_is_still_a_decodable_jpeg():
    clean = strip_jpeg_metadata(_jpeg_with_exif())
    assert clean[:2] == b"\xff\xd8"
    img = Image.open(io.BytesIO(clean))
    img.load()
    assert img.format == "JPEG"


def test_jpeg_without_metadata_is_left_essentially_alone():
    """A frame with nothing to strip must still come out valid."""
    original = _plain_jpeg()
    clean = strip_jpeg_metadata(original)
    a = Image.open(io.BytesIO(original)).convert("RGB")
    b = Image.open(io.BytesIO(clean)).convert("RGB")
    assert list(a.getdata()) == list(b.getdata())


def test_comment_segment_is_removed():
    """COM is free text — a plausible place for a note about the location."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (5, 5, 5)).save(
        buf, "JPEG", comment=b"shot outside 195 Spadina Ave"
    )
    original = buf.getvalue()
    assert b"195 Spadina Ave" in original

    clean = strip_jpeg_metadata(original)
    assert b"195 Spadina Ave" not in clean
    Image.open(io.BytesIO(clean)).load()


def test_icc_profile_is_preserved():
    """APP2 is colour fidelity, not PII — dropping it would shift colours."""
    icc = b"\x00\x00\x02\x0cADBE" + b"\x00" * 100  # plausible-looking stub
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (9, 9, 9)).save(buf, "JPEG", icc_profile=icc)
    original = buf.getvalue()

    clean = strip_jpeg_metadata(original)
    assert b"ICC_PROFILE" in clean


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\xff",
        b"\xff\xd8",
        b"not a jpeg at all",
        b"\xff\xd8\xff\xe1",              # APP1 marker with no length
        b"\xff\xd8\xff\xe1\x00\x01",      # nonsense segment length
        b"\xff\xd8\xff\xe1\xff\xff\x00",  # length overruns the buffer
    ],
)
def test_malformed_input_is_returned_not_raised(blob):
    """Conservative by design: a frame is never lost because parsing failed."""
    out = strip_jpeg_metadata(blob)
    assert isinstance(out, bytes)


# ── EXIF Orientation, applied before the strip ────────────────────────────────
#
# The strip drops APP1, which is where Orientation lives. For this system's Android
# client that is a no-op -- it writes no EXIF at all -- but the pixel buffer's own
# shape is the ONLY orientation record the system has, so a source that did write the
# tag would have its only record silently deleted and would then display sideways.
#
# 20 frames from a pre-2026-08-19 client build are sideways on disk for the
# neighbouring reason and had to be corrected by hand. These tests cover the version
# of that hole a tag-writing source would fall into.


def _jpeg_with_orientation(value: int, size=(24, 16)) -> bytes:
    img = Image.new("RGB", size, (200, 40, 40))
    exif = img.getexif()
    exif[0x0112] = value
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif, quality=90)
    return buf.getvalue()


def test_orientation_is_read_before_it_is_stripped():
    assert exif_orientation(_jpeg_with_orientation(6)) == 6
    assert exif_orientation(_jpeg_with_orientation(1)) == 1


def test_a_frame_with_no_exif_reports_no_orientation():
    # The whole corpus. Must not be confused with "orientation 1".
    assert exif_orientation(_plain_jpeg()) is None


def test_the_no_exif_path_returns_the_identical_object():
    """`is`, not `==`: the common case must not even copy, let alone re-encode.

    The strip is a byte-level segment walk with no decode, deliberately. If applying
    orientation decoded every frame it would put a JPEG round-trip in the ingestion
    path for the 100% of frames that do not need one.
    """
    plain = _plain_jpeg()
    assert apply_exif_orientation(plain) is plain

    upright = _jpeg_with_orientation(1)
    assert apply_exif_orientation(upright) is upright


def test_orientation_6_rotates_the_pixels():
    """A 24x16 landscape frame tagged "rotate 90" must come out 16x24 portrait."""
    out = apply_exif_orientation(_jpeg_with_orientation(6, size=(24, 16)))
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (16, 24)


def test_the_applied_orientation_survives_the_strip():
    """End to end, in the order frame_service calls them."""
    stored = strip_jpeg_metadata(apply_exif_orientation(_jpeg_with_orientation(6)))
    assert b"Exif\x00\x00" not in stored          # the tag is gone, as intended
    with Image.open(io.BytesIO(stored)) as im:
        assert im.size == (16, 24)                # but its meaning was kept


def test_stripping_first_would_have_lost_it():
    """The bug this guards against, asserted directly.

    Reversing the two calls throws the rotation away: the tag is stripped, so
    apply_exif_orientation then has nothing to act on and the pixels stay landscape.
    """
    wrong_order = apply_exif_orientation(strip_jpeg_metadata(_jpeg_with_orientation(6)))
    with Image.open(io.BytesIO(wrong_order)) as im:
        assert im.size == (24, 16)  # still sideways — which is the point


@pytest.mark.parametrize("blob", [b"", b"\xff\xd8", b"\xff\xd8\xff", b"not a jpeg at all"])
def test_orientation_never_raises_on_junk(blob):
    assert exif_orientation(blob) is None
    assert apply_exif_orientation(blob) is blob
