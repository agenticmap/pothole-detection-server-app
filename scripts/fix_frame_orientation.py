"""Rotate the sideways frames upright, and move their boxes with them.

WHY THIS EXISTS. 20 of the 7,615 stored frames are 640x480 landscape buffers
holding a scene rotated 90 degrees clockwise-from-upright: sky fills the left two
thirds, the road runs vertically up the right edge. They were uploaded by a
pre-2026-08-19 app build, before `MapsCameraBinder` set
`setOutputImageRotationEnabled(true)`; the app repo records it as "anything already
ingested was sideways". The cutover is clean -- last landscape 2026-08-18 20:28,
first portrait 2026-08-19 13:15.

EXIF IS NOT INVOLVED. The app writes no EXIF at all (Bitmap.compress emits JFIF +
ICC only), so there is no Orientation tag to read and none was ever destroyed. The
system has exactly ONE orientation record: the pixel buffer's own shape. That is why
the fix is to change the pixels, and why the selector below is a shape test.

WHY AT REST RATHER THAN ON DISPLAY. Every box coordinate in the database --
`frame_box`, `server_detections`, `device_detections` -- is normalized against the
stored pixel buffer. A display-time rotation would need the same transform applied in
three places plus its inverse in the drag-to-draw path, and would leave the detector
scoring sideways pixels through an ROI that only ever trims ROWS. Rotating at rest
keeps one orientation invariant across the whole corpus.

WHAT THIS DOES NOT DO. It does not transform `server_detections`. Those boxes came
from an ROI crop that was sampling half sky -- the crop assumes "up" is -y and on
these frames the road/sky split runs along x, so it could not isolate the road at
all. Regenerating them is the only honest option: this script clears `detected_at`
for exactly the frames it rotated, and `backfill_detection.py` rescores them.

Usage (from the repo root):

    python scripts/fix_frame_orientation.py --dry-run
    python scripts/fix_frame_orientation.py

    # then, to regenerate detections at the corpus's own operating point:
    python scripts/backfill_detection.py --model models/yolo11s_pothole_v1.onnx \\
        --model-id yolo11s_pothole_v1 --conf 0.05 --iou 0.45 \\
        --roi --roi-top 0.45 --roi-bottom 0.90
"""

from __future__ import annotations

import argparse
import asyncio
import io
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.frame_service import resolve_local_frame_path  # noqa: E402

# The shape that identifies an unrotated frame. Derived rather than a hard-coded id
# list so a rerun is safe and a frame missed the first time is still caught -- and so
# this keeps working if another pre-fix build's frames ever surface.
LANDSCAPE = (640, 480)
PORTRAIT = (480, 640)

_FRAMES_SQL = """
SELECT client_id, jpeg_url FROM asset_frame
WHERE jpeg_url IS NOT NULL
ORDER BY client_id
"""

# Scoped to the ids this script actually rotated. backfill_detection's own
# _CLEAR_DETECTED_SQL has NO WHERE clause -- it nulls detected_at for every frame in
# the table -- so it must not be borrowed here.
_CLEAR_DETECTED_SQL = """
UPDATE asset_frame
SET detected_at = NULL, server_probability = NULL, server_model_id = NULL,
    server_detections = NULL
WHERE client_id = ANY($1::text[])
"""

_BOXES_SQL = "SELECT id, x, y, w, h FROM frame_box WHERE frame_client_id = ANY($1::text[])"

_UPDATE_BOX_SQL = "UPDATE frame_box SET x = $2, y = $3, w = $4, h = $5 WHERE id = $1"


def rotate_box_cw(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    """Rotate a normalized corner-origin box 90 degrees clockwise.

    A point (x, y) in the source maps to (1 - y, x) in the rotated image. Applying
    that to both corners of the box and re-deriving the origin gives:

        (x, y, w, h) -> (1 - y - h, x, h, w)

    The width and height swap, which is what keeps this consistent with the image's
    own dimensions swapping. `frame_box`'s CHECK (x + w <= 1.0001 AND y + h <= 1.0001)
    still holds because the two sums trade places: the new x + w is 1 - y, and the new
    y + h is the old x + w.

    Four applications return the original box, which is what the spec asserts.
    """
    return (1.0 - y - h, x, h, w)


def _dimensions(path: Path) -> tuple[int, int] | None:
    """Read (width, height) from the JPEG's SOF marker without decoding the image.

    Deliberately header-only: this runs over every row in asset_frame, and decoding
    7,615 JPEGs to learn their shape would be minutes of work for two integers.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    i = 2  # skip SOI
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0/1/2/3, 5-7, 9-11, 13-15 all carry the frame header. Skip DHT/DQT/etc.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                      0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return None
        i += 2 + length
    return None


def _rotate_file(path: Path, *, backup_dir: Path, dry_run: bool) -> str:
    """Rotate one JPEG 90 degrees clockwise on disk. Returns how it was done."""
    from PIL import Image

    if dry_run:
        return "dry-run"

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / path.name
    if not backup.exists():  # never overwrite a backup on a rerun
        shutil.copy2(path, backup)

    # Read, rotate and encode ENTIRELY IN MEMORY before going near the file again.
    #
    # The first version of this did `rotated.save(path)` inside `with Image.open(path)`,
    # which truncated the source while its handle was still open -- Pillow's save opens
    # the target "w+b". The save then raised on an unrelated argument error and left a
    # zero-length JPEG. The backup made that recoverable, which is the only reason it
    # was a scare rather than a lost training frame. Never write to a path you are
    # still reading.
    with Image.open(path) as im:
        # PIL rotates counter-clockwise, so ROTATE_270 is 90 clockwise.
        rotated = im.transpose(Image.Transpose.ROTATE_270)

    buf = io.BytesIO()
    # 640x480 is a clean multiple of the 8x8 MCU grid, so a LOSSLESS transform is
    # possible with `jpegtran -rotate 90`. Pillow has no lossless path and jpegtran is
    # not a dependency, so this re-encodes: quality 95 at 4:4:4 (subsampling=0), the
    # least-lossy sensible setting. These are training inputs, so the generation loss
    # is worth stating rather than hiding. Originals stay in backup_dir.
    rotated.save(buf, format="JPEG", quality=95, subsampling=0)
    data = buf.getvalue()

    # Atomic replace, mirroring _store_jpeg_local: a crash mid-write must not be able
    # to leave a partial frame where a valid one was.
    tmp = path.with_suffix(".jpg.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return "re-encoded q95 4:4:4"


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change and touch nothing")
    p.add_argument("--backup-dir", default="storage/frames-preroration-backup",
                   help="where the untouched originals are copied before rotation")
    args = p.parse_args()

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_FRAMES_SQL)

            sideways: list[tuple[str, Path]] = []
            shapes: dict[tuple[int, int], int] = {}
            unreadable = 0
            for r in rows:
                try:
                    path = resolve_local_frame_path(r["jpeg_url"])
                except ValueError:
                    unreadable += 1
                    continue
                dims = _dimensions(path)
                if dims is None:
                    unreadable += 1
                    continue
                shapes[dims] = shapes.get(dims, 0) + 1
                if dims == LANDSCAPE:
                    sideways.append((r["client_id"], path))

            print(f"database={settings.database_url.rsplit('/', 1)[-1]}")
            print(f"frames with a jpeg_url: {len(rows)}   unreadable: {unreadable}")
            print("shapes on disk:")
            for dims, n in sorted(shapes.items(), key=lambda kv: -kv[1]):
                tag = "  <- portrait, correct" if dims == PORTRAIT else ""
                tag = "  <- SIDEWAYS, will rotate 90 CW" if dims == LANDSCAPE else tag
                print(f"  {dims[0]}x{dims[1]}: {n}{tag}")

            if not sideways:
                print("\nNothing to do -- every frame is already portrait.")
                return 0

            ids = [cid for cid, _ in sideways]
            print(f"\n{len(sideways)} frame(s) to rotate:")
            for cid in ids:
                print(f"  {cid}")

            boxes = await conn.fetch(_BOXES_SQL, ids)
            print(f"\nhuman boxes on those frames: {len(boxes)} (will be rotated with the pixels)")

            if args.dry_run:
                print("\n--dry-run: nothing was written.")
                return 0

            # ── Rotate the pixels ────────────────────────────────────────────────
            backup_dir = Path(args.backup_dir)
            how = ""
            for cid, path in sideways:
                how = _rotate_file(path, backup_dir=backup_dir, dry_run=False)
                after = _dimensions(path)
                if after != PORTRAIT:
                    print(f"  FAILED {cid}: still {after} after rotation", file=sys.stderr)
                    return 1
            print(f"\nRotated {len(sideways)} file(s) ({how}); originals in {backup_dir}/")

            # ── Move the boxes with them, in one transaction ─────────────────────
            async with conn.transaction():
                for b in boxes:
                    nx, ny, nw, nh = rotate_box_cw(b["x"], b["y"], b["w"], b["h"])
                    await conn.execute(_UPDATE_BOX_SQL, b["id"], nx, ny, nw, nh)
                # Regenerate rather than transform: the old detections came from an
                # ROI crop that could not isolate the road on a rotated frame.
                cleared = await conn.execute(_CLEAR_DETECTED_SQL, ids)
            print(f"Rotated {len(boxes)} human box(es).")
            print(f"Cleared detections so they are rescored ({cleared}).")
            print("\nNext: backfill_detection.py --conf 0.05 (the corpus's operating point)")
            return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
