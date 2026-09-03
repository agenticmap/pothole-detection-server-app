"""Lossless removal of JPEG metadata segments.

Road frames are photographs of public streets: they contain licence plates,
faces and house numbers, and phone cameras stamp GPS coordinates into EXIF.
Every stored frame is served to any `viewer` staff account via
GET /api/v1/frames/{client_id}/image, so the metadata is a wider exposure than
the pixels alone.

This strips the metadata segments rather than re-encoding through an image
library: it is exact (the entropy-coded scan data is copied byte for byte, so
there is no generation loss and no quality setting to get wrong), it is fast
enough to sit in the ingestion path, and it needs no new dependency.

It does not touch the pixels. Redacting plates and faces from the image itself
is a detection model, not a parser, and is out of scope here.
"""

import logging

logger = logging.getLogger(__name__)

# Segments dropped. APP1 is the important one — it carries both EXIF (with the
# GPS IFD) and XMP, either of which can hold a location.
#
# Kept on purpose: APP0 (JFIF density, harmless and some decoders expect it),
# APP2 (ICC colour profile — dropping it can visibly shift colour), and APP14
# (Adobe, which governs colour-transform interpretation for some encoders).
_STRIP_MARKERS = frozenset(
    {0xE1}                          # APP1  — EXIF / XMP
    | set(range(0xE3, 0xED))        # APP3..APP12 — maker notes and friends
    | {0xED}                        # APP13 — Photoshop / IPTC
    | {0xEF}                        # APP15
    | {0xFE}                        # COM   — free-text comment
)

# Markers that stand alone, carrying no length field or payload.
_STANDALONE = frozenset({0x01} | set(range(0xD0, 0xD8)))

_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA


def exif_orientation(jpeg: bytes) -> int | None:
    """The EXIF Orientation tag (0x0112), or None if there isn't one.

    Read BEFORE stripping, because stripping APP1 destroys it. See
    `apply_exif_orientation` for why that matters.

    Deliberately a hand-rolled TIFF walk rather than a Pillow decode: this sits in
    the ingestion path, the overwhelming majority of frames have no EXIF at all, and
    the whole point of this module is that the common case never decodes an image.
    Returns None on anything it cannot parse — an unreadable tag is treated as an
    absent one, which is the same conservative posture as the stripper.
    """
    i, n = 2, len(jpeg)
    while i + 3 < n:
        if jpeg[i] != 0xFF:
            return None
        marker = jpeg[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in _STANDALONE or marker in (_EOI, _SOS):
            return None
        seg_len = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        if seg_len < 2 or i + 2 + seg_len > n:
            return None
        if marker == 0xE1 and jpeg[i + 4 : i + 10] == b"Exif\x00\x00":
            return _orientation_from_tiff(jpeg[i + 10 : i + 2 + seg_len])
        i += 2 + seg_len
    return None


def _orientation_from_tiff(tiff: bytes) -> int | None:
    """Find tag 0x0112 in IFD0 of a TIFF header. Endianness comes from the header."""
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        order = "little"
    elif tiff[:2] == b"MM":
        order = "big"
    else:
        return None
    ifd0 = int.from_bytes(tiff[4:8], order)  # type: ignore[arg-type]
    if ifd0 + 2 > len(tiff):
        return None
    count = int.from_bytes(tiff[ifd0 : ifd0 + 2], order)  # type: ignore[arg-type]
    for k in range(count):
        entry = ifd0 + 2 + k * 12
        if entry + 12 > len(tiff):
            return None
        if int.from_bytes(tiff[entry : entry + 2], order) == 0x0112:  # type: ignore[arg-type]
            # SHORT value, left-aligned in the 4-byte value field.
            value = int.from_bytes(tiff[entry + 8 : entry + 10], order)  # type: ignore[arg-type]
            return value if 1 <= value <= 8 else None
    return None


def apply_exif_orientation(jpeg: bytes) -> bytes:
    """Bake an EXIF Orientation into the pixels, so stripping cannot lose it.

    WHY. `strip_jpeg_metadata` drops APP1, which carries the Orientation tag. For a
    frame whose pixels are already upright and whose tag says "no rotation" that is a
    no-op — and that is every frame this system has ever ingested, because the Android
    client writes no EXIF at all (`Bitmap.compress` emits JFIF + ICC only). But the
    system has exactly ONE orientation record, the pixel buffer's own shape, so any
    future source that DOES write the tag — a gallery import, a different phone SDK, a
    dashcam — would have its only record silently deleted and would then display
    sideways with nothing left to recover it from.

    That is not hypothetical: 20 frames from a pre-2026-08-19 client build are sideways
    on disk for the neighbouring reason (the app never rotated the pixels and never
    wrote a tag either), and they had to be corrected by hand with
    `scripts/fix_frame_orientation.py`. This closes the version of that hole that a
    tag-writing source would fall into.

    Costs nothing in the common case: with no Orientation tag, or with Orientation 1
    ("already upright"), the bytes are returned unchanged and Pillow is never imported.
    """
    orientation = exif_orientation(jpeg)
    if orientation is None or orientation == 1:
        return jpeg
    try:
        import io

        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(jpeg)) as im:
            upright = ImageOps.exif_transpose(im)
            if upright is None:
                return jpeg
            buf = io.BytesIO()
            # 95 at 4:4:4, matching scripts/fix_frame_orientation.py. This path
            # re-encodes and therefore loses a generation, which is the price of not
            # losing the orientation entirely.
            upright.save(buf, format="JPEG", quality=95, subsampling=0)
        logger.info("Applied EXIF orientation %d to a frame before stripping", orientation)
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001 — never fail ingestion over an orientation
        logger.warning("Could not apply EXIF orientation %d (%s); storing as-is.",
                       orientation, e)
        return jpeg


def strip_jpeg_metadata(jpeg: bytes) -> bytes:
    """Return `jpeg` without its metadata segments.

    Conservative by construction: anything unparseable makes it copy the
    remainder verbatim and return, so a frame is never corrupted or dropped just
    because this could not make sense of it. Callers have already validated the
    JPEG magic bytes.

    **Call `apply_exif_orientation` first.** This drops APP1, which is where the
    Orientation tag lives, and nothing downstream can recover it.
    """
    if len(jpeg) < 4 or jpeg[0] != 0xFF or jpeg[1] != _SOI:
        return jpeg

    out = bytearray(jpeg[:2])
    i, n = 2, len(jpeg)

    while i + 1 < n:
        if jpeg[i] != 0xFF:
            # Not on a marker boundary — stop interpreting and keep the rest.
            out += jpeg[i:]
            return bytes(out)

        marker = jpeg[i + 1]

        # Fill bytes: any number of 0xFF may pad before the marker itself.
        if marker == 0xFF:
            out.append(0xFF)
            i += 1
            continue

        if marker in _STANDALONE or marker == _EOI:
            out += jpeg[i : i + 2]
            i += 2
            continue

        if marker == _SOS:
            # Entropy-coded scan data follows and is not length-prefixed. Copy
            # byte for byte; this is what makes the operation lossless.
            out += jpeg[i:]
            return bytes(out)

        if i + 4 > n:
            out += jpeg[i:]
            return bytes(out)

        seg_len = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        if seg_len < 2 or i + 2 + seg_len > n:
            out += jpeg[i:]
            return bytes(out)

        if marker not in _STRIP_MARKERS:
            out += jpeg[i : i + 2 + seg_len]
        i += 2 + seg_len

    if i < n:
        out += jpeg[i:]
    return bytes(out)
