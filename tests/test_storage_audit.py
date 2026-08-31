"""The frame-store reconciler, and the guard that stops it destroying the archive.

`--delete-orphans` decides what to delete by asking the database what it knows
about. Against a TRUNCATEd database it knows about nothing, so every real frame
looks like an orphan. `pothole_test` is TRUNCATEd by the fixtures in this very
suite, which makes "run the audit after a test run" a plausible and catastrophic
mistake.

That guard is the reason this file exists. The reconciliation logic is tested
alongside it because the guard is only worth having if the thing it guards works.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "storage_audit",
    Path(__file__).resolve().parent.parent / "scripts" / "storage_audit.py",
)
audit = importlib.util.module_from_spec(_SPEC)
sys.modules["storage_audit"] = audit
_SPEC.loader.exec_module(audit)


def _write(root: Path, rel: str, content: bytes = b"jpeg") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ── The guard ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("db", ["pothole_test", "pothole_ci"])
@pytest.mark.parametrize("flag", ["--delete-orphans", "--delete-demo"])
def test_deletion_refuses_a_scratch_database(db, flag, monkeypatch, capsys):
    """The whole point: never delete files based on a database the tests wipe."""
    monkeypatch.setattr(
        audit.settings, "database_url", f"postgresql://u:p@localhost:5433/{db}", raising=False
    )
    monkeypatch.setattr(sys, "argv", ["storage_audit.py", flag])

    # argparse's error() exits 2; the database is never opened, which is what
    # matters -- create_pool is not reached.
    with pytest.raises(SystemExit) as exc:
        import asyncio

        asyncio.run(audit.main())
    assert exc.value.code == 2
    assert "Refusing to delete" in capsys.readouterr().err


def test_scratch_database_names_cover_the_conftest_allow_list():
    """If conftest gains a scratch database, this guard must learn about it too.

    Otherwise a new test database would be deletable-from, silently.
    """
    from tests.conftest import _ALLOWED_TEST_DATABASES

    assert _ALLOWED_TEST_DATABASES <= audit._SCRATCH_DATABASES


# ── Reconciliation ──────────────────────────────────────────────────────────


def test_scan_disk_keys_match_the_jpeg_url_convention(tmp_path):
    """Keys must be POSIX-relative, because that is what _store_jpeg_local writes.

    A Windows scan producing backslashes would make every row look dangling and
    every file look orphaned at the same time.
    """
    _write(tmp_path, "device-a/frame-1.jpg")
    _write(tmp_path, "device-b/nested/frame-2.jpg")

    found = audit._scan_disk(tmp_path)

    assert set(found) == {"device-a/frame-1.jpg", "device-b/nested/frame-2.jpg"}
    assert all("\\" not in key for key in found)


def test_scan_disk_tolerates_a_missing_root(tmp_path):
    assert audit._scan_disk(tmp_path / "not-created-yet") == {}


def test_report_separates_orphans_dangling_and_demo(tmp_path, capsys):
    _write(tmp_path, "dev-1/kept.jpg")
    _write(tmp_path, "dev-1/orphan.jpg")
    _write(tmp_path, "demo-dev-01/seeded.jpg")
    rows = {
        "dev-1/kept.jpg": "frm_kept",
        "dev-1/gone.jpg": "frm_dangling",  # row with no file
    }

    orphans, demo = audit._report(audit._scan_disk(tmp_path), rows, tmp_path)

    assert orphans == ["dev-1/orphan.jpg"]
    # A demo file has no row either, but it is reported under its own heading
    # rather than double-counted as an orphan.
    assert demo == ["demo-dev-01/seeded.jpg"]
    assert "demo-dev-01/seeded.jpg" not in orphans

    out = capsys.readouterr().out
    assert "Dangling rows (no file)   :      1" in out
    assert "frm_dangling" in out


def test_delete_removes_files_and_prunes_emptied_directories(tmp_path, capsys):
    _write(tmp_path, "dev-1/keep.jpg")
    _write(tmp_path, "demo-dev-01/a.jpg")
    _write(tmp_path, "demo-dev-01/b.jpg")

    audit._delete(["demo-dev-01/a.jpg", "demo-dev-01/b.jpg"], tmp_path, "demo")

    assert (tmp_path / "dev-1/keep.jpg").exists()
    # The directory goes too -- otherwise the store accumulates empty per-device
    # trees that still look like devices to anything counting directories.
    assert not (tmp_path / "demo-dev-01").exists()
    assert "Deleted 2 demo file(s)" in capsys.readouterr().out


def test_delete_never_touches_a_referenced_file(tmp_path):
    """Belt and braces: _report is what decides, but prove _delete is not clever."""
    kept = _write(tmp_path, "dev-1/kept.jpg")
    orphan = _write(tmp_path, "dev-1/orphan.jpg")
    rows = {"dev-1/kept.jpg": "frm_kept"}

    orphans, _ = audit._report(audit._scan_disk(tmp_path), rows, tmp_path)
    audit._delete(orphans, tmp_path, "orphan")

    assert kept.exists()
    assert not orphan.exists()
