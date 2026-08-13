# Server model attribution

The server-side detection worker (Phase 2.3, see `docs/phase-2.3-detection-plan.md`) runs a
**user-supplied** model — none is bundled in this repo. This file records its provenance once
one is dropped in, mirroring the app repo's `docs/model-attribution.md`.

## Detection model (`DETECTION_BACKEND=onnx`)

| | |
| --- | --- |
| Architecture | YOLOv8-small/medium (Ultralytics), ONNX export |
| `model_id` | `yolov8s_pothole_v1` (set via `DETECTION_MODEL_ID`) |
| Input | letterboxed `DETECTION_INPUT_SIZE` (default 640) RGB, float32 `[1,3,H,W]` |
| Expected output | `[1, 4+nc, N]` — post-sigmoid class scores (Ultralytics default export) |
| File | path given by `DETECTION_MODEL_PATH` (not committed; gitignored alongside `storage/`) |
| Dataset | _fill in — e.g. the same Roboflow set as the on-device model, for fusion consistency_ |
| License | _fill in_ |
| SHA-256 | _fill in after export_ |

> For fusion math consistency, train/fine-tune on the **same dataset** as the on-device
> YOLOv8n so `server_probability` and `device_probability` are comparable.

### Export (Ultralytics)
```
yolo export model=best.pt format=onnx imgsz=640 opset=12
```

## External backend (`DETECTION_BACKEND=http`)

If inference is offloaded (Modal / Replicate / Triton), record the endpoint, model version,
and provider here instead. The endpoint must accept a raw JPEG body and return
`{"probability": float, "detections": [...], "model_id": str?}`.
