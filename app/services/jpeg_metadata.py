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


def strip_jpeg_metadata(jpeg: bytes) -> bytes:
    """Return `jpeg` without its metadata segments.

    Conservative by construction: anything unparseable makes it copy the
    remainder verbatim and return, so a frame is never corrupted or dropped just
    because this could not make sense of it. Callers have already validated the
    JPEG magic bytes.
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
