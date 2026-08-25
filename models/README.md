# Detector weights

Server-side detection (`DETECTION_BACKEND=onnx`) loads an ONNX export from here.
**The weights themselves are gitignored** — `.onnx` files are tens of MB, they are
build outputs rather than source, and this repo's history has already had to be
rewritten once to strip large binaries (see `docs/phase-2.6-hardening.md`
"Repo hygiene"). Only this README is tracked, so the directory exists for
`docker-compose.yml`'s read-only mount on a fresh clone.

## What goes here

An Ultralytics export produced with the line pinned in
[`docs/model-attribution.md`](../docs/model-attribution.md):

```
yolo export model=best.pt format=onnx imgsz=640 opset=12 nms=False
```

`nms=False` is not optional. `app/detection/onnx_v1.py` decodes the raw
`[1, 4+nc, N]` graph itself; an export with NMS baked in has shape `[1, 300, 6]`
and would decode to garbage, so `_check_layout` rejects it with the correct
re-export command.

## Then

```
DETECTION_ENABLED=true
DETECTION_BACKEND=onnx
DETECTION_MODEL_PATH=models/<your-export>.onnx
DETECTION_MODEL_ID=<something versioned, e.g. yolo11s_pothole_v1>
```

Prove it before enabling anything — this writes nothing:

```
python scripts/detect_eval.py --model models/<your-export>.onnx --limit 20 --annotate out/
```

Record what the file actually is in `docs/model-attribution.md`. A `server_model_id`
that cannot be traced back to a dataset and a licence is not auditable.
