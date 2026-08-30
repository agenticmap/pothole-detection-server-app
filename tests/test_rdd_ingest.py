"""RDD2022 → Model A conversion: the class remap and the VOC→YOLO geometry.

Two silent-failure modes this guards against, both of which produce a plausible
dataset rather than an error:

- **A wrong class remap** trains "crack" images as "pothole". Nothing downstream can
  detect that; it surfaces months later as a detector that fires on hairlines.
- **A wrong corner→centre conversion** offsets every box by half its own size. The
  model still trains, still reports a mAP, and is simply worse — which reads as a
  modelling problem rather than a data bug.

RDD boxes come in Pascal-VOC *pixel corners*; Model A stores *normalized corners*;
YOLO wants *normalized centres*. Three conventions, so the conversion is worth
pinning down.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ingest_rdd2022",
    Path(__file__).resolve().parent.parent / "scripts" / "ingest_rdd2022.py",
)
ingest = importlib.util.module_from_spec(_SPEC)
sys.modules["ingest_rdd2022"] = ingest
_SPEC.loader.exec_module(ingest)


class TestClassRemap:
    """RDD annotates damage; Model A detects road-surface objects. Only two of its
    four codes map onto ours, and the mapping is not one-to-one."""

    def test_pothole_maps_to_the_primary_class(self):
        from app.detection.classes import PRIMARY_CLASS_ID, ROAD_SURFACE_CLASSES

        assert ingest._REMAP["D40"] == "pothole"
        assert ROAD_SURFACE_CLASSES[PRIMARY_CLASS_ID] == "pothole"

    @pytest.mark.parametrize("code", ["D00", "D10", "D20"])
    def test_every_crack_subtype_collapses_into_one_class(self, code):
        """Longitudinal, transverse and alligator cracking all become `crack`.

        Model A does not report crack *type*, and splitting a data-poor class three
        ways is precisely how v4 failed: 30/7/6-box classes were too weak to be
        selective and stole detections from pothole.
        """
        assert ingest._REMAP[code] == "crack"

    def test_manhole_grate_patch_are_not_sourced_from_rdd(self):
        """RDD has no equivalent, so those classes must keep coming from our own
        labels. A remap that silently invented them would poison the classes we
        actually hand-drew."""
        assert set(ingest._REMAP.values()) == {"pothole", "crack"}

    def test_every_remap_target_is_a_real_model_a_class(self):
        from app.detection.classes import ROAD_SURFACE_CLASSES

        assert set(ingest._REMAP.values()) <= set(ROAD_SURFACE_CLASSES)

    def test_the_label_map_is_covered_completely(self):
        """RDD2022's label_map.pbtxt defines exactly D00, D10, D20, D40. If a future
        release adds a code, this fails rather than silently dropping it."""
        assert set(ingest._REMAP) == {"D00", "D10", "D20", "D40"}


class TestVocToYolo:
    W, H = 600, 600

    def _line(self, xmin, ymin, xmax, ymax, class_id=0, w=None, h=None):
        return ingest._to_yolo_line(class_id, xmin, ymin, xmax, ymax,
                                    w or self.W, h or self.H)

    def test_centre_and_extent_are_correct(self):
        # A 120x60 box at (60,150)-(180,210): centre (120,180), i.e. 0.2, 0.3.
        parts = self._line(60, 150, 180, 210).split()
        assert parts[0] == "0"
        assert [float(v) for v in parts[1:]] == pytest.approx([0.2, 0.3, 0.2, 0.1])

    def test_class_id_is_carried_through(self):
        assert self._line(60, 150, 180, 210, class_id=4).split()[0] == "4"

    def test_it_round_trips_against_the_exporter(self):
        """The exporter owns the only other corner↔centre conversion in the repo.
        If these two ever disagree, half the dataset is offset and nothing errors."""
        spec = importlib.util.spec_from_file_location(
            "export_labeled_frames",
            Path(__file__).resolve().parent.parent / "scripts" / "export_labeled_frames.py",
        )
        exporter = importlib.util.module_from_spec(spec)
        sys.modules["export_labeled_frames"] = exporter
        spec.loader.exec_module(exporter)

        _, cx, cy, w, h = (float(v) if i else int(v) for i, v in
                           enumerate(self._line(60, 150, 180, 210).split()))
        corner = exporter._from_yolo(0, cx, cy, w, h)
        assert corner["x"] == pytest.approx(60 / self.W)
        assert corner["y"] == pytest.approx(150 / self.H)
        assert corner["w"] == pytest.approx(120 / self.W)
        assert corner["h"] == pytest.approx(60 / self.H)

    def test_a_box_running_past_the_edge_is_clamped_not_dropped(self):
        """RDD contains boxes a pixel or two outside the image. YOLO accepts
        out-of-range coordinates silently, so clamp rather than pass them on."""
        parts = [float(v) for v in self._line(-5, -5, 120, 120).split()[1:]]
        cx, cy, w, h = parts
        assert 0.0 < cx < 1.0 and 0.0 < cy < 1.0
        assert cx - w / 2 >= -1e-6 and cy - h / 2 >= -1e-6

    def test_a_hairline_box_is_dropped(self):
        """Sub-threshold boxes are cracks a windshield camera cannot resolve at
        speed. Training on them teaches "undamaged asphalt is a crack" -- the same
        suppression that cost pothole recall in v2/v3."""
        assert self._line(300, 300, 302, 303) is None

    def test_a_degenerate_box_is_dropped(self):
        assert self._line(300, 300, 300, 400) is None
        assert self._line(300, 300, 400, 300) is None

    def test_a_full_frame_box_survives(self):
        parts = [float(v) for v in self._line(0, 0, self.W, self.H).split()[1:]]
        assert parts == pytest.approx([0.5, 0.5, 1.0, 1.0])

    def test_non_square_images_normalize_per_axis(self):
        """RDD resolutions vary by country; using one axis for both is a classic
        way to squash every box."""
        parts = [float(v) for v in
                 self._line(0, 0, 300, 200, w=600, h=400).split()[1:]]
        assert parts == pytest.approx([0.25, 0.25, 0.5, 0.5])


def test_voc_parsing_reads_size_and_objects(tmp_path):
    xml = tmp_path / "sample.xml"
    xml.write_text(
        """<annotation>
             <size><width>720</width><height>720</height><depth>3</depth></size>
             <object><name>D40</name>
               <bndbox><xmin>10</xmin><ymin>20</ymin><xmax>110</xmax><ymax>120</ymax></bndbox>
             </object>
             <object><name>D00</name>
               <bndbox><xmin>200</xmin><ymin>210</ymin><xmax>300</xmax><ymax>260</ymax></bndbox>
             </object>
           </annotation>""",
        encoding="utf-8",
    )
    width, height, boxes = ingest._parse_voc(xml)
    assert (width, height) == (720, 720)
    assert [b[0] for b in boxes] == ["D40", "D00"]
    assert boxes[0][1:] == (10.0, 20.0, 110.0, 120.0)


def test_an_annotation_with_no_objects_is_a_background(tmp_path):
    """RDD includes undamaged road. Those are legitimate background images -- and
    unlike our own frames, they are known-reviewed by construction."""
    xml = tmp_path / "empty.xml"
    xml.write_text(
        "<annotation><size><width>600</width><height>600</height></size></annotation>",
        encoding="utf-8",
    )
    _, _, boxes = ingest._parse_voc(xml)
    assert boxes == []
