"""Which frames the labelling tool puts in front of you.

This decides whether a review pass shows outstanding work or work already finished,
and it used to live inline in `_load_queue` tangled up with database I/O and JPEG
resolution, so nothing could test it. The bug that prompted extracting it: the browser
kept showing 174 frames when 20 were outstanding.

The rule worth protecting here is `--review`. It is a check-my-work mode, NOT an
"include everything" switch -- mixing finished and outstanding frames in one queue
means paging past completed work to reach the next real frame.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "label_frames",
    Path(__file__).resolve().parent.parent / "scripts" / "label_frames.py",
)
label_frames = importlib.util.module_from_spec(_SPEC)
sys.modules["label_frames"] = label_frames
_SPEC.loader.exec_module(label_frames)

_wants_frame = label_frames._wants_frame


def row(label=None, boxed_at=None):
    """The two columns the filter reads, named as _SELECT_SQL returns them."""
    return {"label": label, "boxed_at": boxed_at}


NOW = "2026-08-29T12:00:00+00:00"


class TestBoxMode:
    """The box-review pass over frames that already carry a verdict."""

    def test_an_unjudged_frame_is_never_queued(self):
        """Drawing a box before deciding what the frame IS anchors the verdict to the
        box. Box mode refuses those in both directions, review or not."""
        assert not _wants_frame(row(label=None), box=True, review=False)
        assert not _wants_frame(row(label=None), box=True, review=True)

    def test_a_judged_unsubmitted_frame_is_the_work(self):
        assert _wants_frame(row(label=0), box=True, review=False)
        assert _wants_frame(row(label=1), box=True, review=False)

    def test_a_submitted_frame_is_out_of_the_way(self):
        """The regression: finished frames stayed in the queue and had to be paged past."""
        assert not _wants_frame(row(label=0, boxed_at=NOW), box=True, review=False)

    def test_review_shows_only_submitted_frames(self):
        assert _wants_frame(row(label=0, boxed_at=NOW), box=True, review=True)

    def test_review_hides_outstanding_frames(self):
        """The whole point of the mode: checking finished work never mixes in work
        that has not been done."""
        assert not _wants_frame(row(label=0), box=True, review=True)

    def test_the_two_modes_partition_the_judged_frames(self):
        """Every judged frame lands in exactly one of the two queues -- no frame is
        unreachable, and none shows up in both."""
        for r in (row(label=1), row(label=0, boxed_at=NOW)):
            normal = _wants_frame(r, box=True, review=False)
            reviewing = _wants_frame(r, box=True, review=True)
            assert normal != reviewing


class TestVerdictMode:
    """`--review` means the same thing in both modes: show me what is already done."""

    def test_unlabelled_frames_are_the_work(self):
        assert _wants_frame(row(), box=False, review=False)

    def test_labelled_frames_drop_out(self):
        assert not _wants_frame(row(label=1), box=False, review=False)

    def test_review_shows_only_labelled_frames(self):
        assert _wants_frame(row(label=1), box=False, review=True)
        assert not _wants_frame(row(), box=False, review=True)

    @pytest.mark.parametrize("label", [1, 0, -1])
    def test_every_verdict_counts_as_done(self, label):
        """Including -1. "I cannot tell" is a decision that was made, not a gap."""
        assert not _wants_frame(row(label=label), box=False, review=False)
        assert _wants_frame(row(label=label), box=False, review=True)


def test_boxes_are_not_what_makes_a_frame_done():
    """`boxed_at`, not the presence of boxes, decides. A frame reviewed and found
    genuinely clean has zero boxes and is still finished; a frame nobody opened also
    has zero boxes and is not. Collapsing those two is the mistake Phase 2.7b exists
    to prevent, so the filter must never reason about box counts."""
    reviewed_and_clean = row(label=0, boxed_at=NOW)
    never_opened = row(label=0)
    assert not _wants_frame(reviewed_and_clean, box=True, review=False)
    assert _wants_frame(never_opened, box=True, review=False)


class TestScoreOrdering:
    """`--order score` puts the densest seam of likely potholes at the top.

    The detector's problem is a shortage of in-domain POSITIVES: three labelling
    passes added ~200 negatives each and each cost recall. Measured on the holdout,
    frames scoring 0.30+ are 62-75% pothole against a 46% base rate, while the
    0.00-0.05 band is 32% -- so ranking, not filtering, is what buys labelling time.
    """

    def rows(self, *scores):
        return [{"client_id": f"f{i}", "server_probability": s}
                for i, s in enumerate(scores)]

    def test_highest_score_first(self):
        out = label_frames._by_score(self.rows(0.1, 0.9, 0.5), 3)
        assert [r["server_probability"] for r in out] == [0.9, 0.5, 0.1]

    def test_it_truncates_to_count(self):
        assert len(label_frames._by_score(self.rows(0.1, 0.9, 0.5, 0.7), 2)) == 2

    def test_unscored_frames_are_excluded_not_ranked_last(self):
        """A NULL score means the backfill has not seen the frame. Sorting it as if it
        were 0.0 would bury real frames under unscored ones."""
        out = label_frames._by_score(self.rows(0.4, None, 0.2), 5)
        assert [r["client_id"] for r in out] == ["f0", "f2"]

    def test_an_entirely_unscored_corpus_returns_nothing(self, capsys):
        """Rather than silently falling back to an arbitrary order -- the operator
        needs to know a backfill has not run."""
        assert label_frames._by_score(self.rows(None, None), 5) == []
        assert "backfill_detection" in capsys.readouterr().err

    def test_scores_never_auto_label(self):
        """The ordering function decides ORDER only. Nothing here may drop a frame for
        scoring low: the bottom band still held 19 of 65 holdout positives, so a
        low-score cutoff would discard most of the data the model most needs."""
        out = label_frames._by_score(self.rows(0.9, 0.001, 0.5), 10)
        assert len(out) == 3
        assert 0.001 in [r["server_probability"] for r in out]
