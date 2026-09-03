---
updated: 2026-09-03
---

# Phase 2.9 — VLM verification: the instrument, and the thresholds it must replace

The hybrid detector (`app/detection/hybrid_v1.py`) has shipped since Phase 2.3b with 21 tests.
**Every one of those tests uses a fake verifier.** No VLM has ever seen a frame from this corpus.

That matters more than it sounds, because a proposed production pipeline puts a VLM in the middle
of it and derives pseudo-labels from its answers. Two of that pipeline's three stages were already
built here; the third rests on threshold numbers that our own labels refute. This phase builds the
missing measurement and fixes what the labels already settle.

Related: [`phase-2.7c-public-data.md`](./phase-2.7c-public-data.md) (why positives are the binding
constraint), [`detection-approach.md`](../architecture/detection-approach.md) (why the design is YOLO → VLM),
[`detection-research-record.md`](../research/detection-research-record.md) (all five detector models).

## 1. The gray zone is measurably wrong

All 5,615 frames are now scored with `yolo11s_pothole_v1`. 340 of them carry a human verdict
(`frame_label.label IN (0,1)`) — 65 pothole, 275 not, **base rate 19.1%**. That is 2.4× the
evidence the 140-frame holdout gave, and it supersedes the band table in Phase 2.7c.

| band | corpus frames | labelled | pothole | rate | lift |
|---|---|---|---|---|---|
| 0.00–0.05 | 2,183 | 164 | 19 | 11.6% | 0.61× |
| 0.05–0.15 | 1,336 | 73 | 18 | 24.7% | 1.29× |
| 0.15–0.30 | 993 | 47 | 12 | 25.5% | 1.34× |
| 0.30–0.40 | 400 | 16 | 6 | **37.5%** | **1.96×** |
| 0.40–0.75 | 698 | 39 | 10 | 25.6% | 1.34× |
| **> 0.75** | **5** | **1** | **0** | **0%** | — |

Max score over the whole corpus is **0.779**; over labelled frames, **0.757**.

**Auto-accept above 0.75 is dead code.** It fires on 5 frames in 5,615 — 0.09% — and the single
labelled frame in that band is a **false positive**. Measured precision of the rule: 0/1.

**Auto-reject below 0.40 would delete the training set.** It covers 4,512 corpus frames (80.4%),
and among labelled frames it discards 300 of 340 holding **55 of the 65 known potholes — 85% of
every in-domain positive we have.** Phase 2.7c identified the positive drought as *the* binding
constraint on this detector. The rule throws away its own cure.

**And the band is the wrong one anyway.** 0.40–0.75 is a 1.34× lift over base rate; 0.30–0.40 is
1.96×. Routing the VLM at 0.40–0.75 spends calls on the *less* informative slice.

**Conclusion, unchanged from 2.7c and now on 2.4× the data: scores prioritise, they never
auto-label.** There is no cut point at which auto-accept or auto-reject is safe for this detector.
`VLM_VERIFY_LOW`/`HIGH` remain shipped at 0.40/0.75 only because the measurement in §3 has not run;
they are labelled in `.env.example` as uncalibrated and are not a review threshold.

## 2. Backends — local and cloud, one client

`ollama`, `openrouter` and `local_http` all speak OpenAI-compatible `POST /v1/chat/completions` with
vision content blocks, so they share `LocalHttpVerifier` and differ only in default URL and whether
a key is demanded. They use stdlib `urllib` — **no extra pip install**.

| `VLM_BACKEND` | endpoint | key | notes |
|---|---|---|---|
| `ollama` | `http://localhost:11434/v1/chat/completions` | no | free, nothing leaves the host |
| `openrouter` | `https://openrouter.ai/api/v1/chat/completions` | **yes** | one key, any hosted VLM |
| `local_http` | `VLM_HTTP_URL` required | optional | vLLM, LM Studio, anything else |
| `claude` | Anthropic SDK | yes | needs `anthropic`; only backend using a hard JSON schema |
| `gemini` | google-genai SDK | yes | needs `google-genai` |

`VLM_HTTP_URL` overrides the default for any of the first three. `VLM_JSON_MODE` sends
`response_format=json_object` (default on; turn off for models that 400 on the field —
`parse_verdict()`'s regex extraction is the fallback either way). `VLM_HTTP_REFERER` /
`VLM_HTTP_TITLE` are OpenRouter attribution headers and are deliberately **not** sent to any other
backend, so a deployment URL cannot leak to whatever `VLM_HTTP_URL` points at.

Two failure modes are now caught early rather than mid-sweep: `openrouter` without a key raises at
construction, and an `HTTPError` carries its **response body** into the message. The status line
alone never distinguishes a bad key from an unknown model id from a text-only model from an
unsupported `response_format`, and `hybrid_v1` logs only `str(e)`.

## 3. `scripts/vlm_eval.py` — the measurement

**Never writes to the database.** Config comes from CLI flags, not `VLM_*` env vars, so a run
cannot silently measure a different model than the flag says. Same contract as `detect_eval.py`.

It scores every labelled frame through the real production path — Stage 1, then
`HybridDetector`'s own `_crop` and `_blend` with the gray zone opened to `[0,1]` so every frame
reaches the verifier — and reports:

1. **The VLM's binary verdict vs ground truth.** Its native output, no threshold. This is the
   number that decides whether pseudo-labelling is admissible at all, and it is printed against the
   base rate, because a precision below 19.1% means the verdict is worse than answering "pothole"
   every time.
2. **Matched-recall curves** for Stage 1 / VLM / blend, using the same `_curve()` as
   `promote_model.py`, so results sit on the same axis as v1/v3/v5.
3. **Score-band tables** for Stage 1 and for the blend — the blended one is what calibrates
   `VLM_VERIFY_LOW`/`HIGH`.
4. **A blend-weight sweep**, recomputed from the cache, so `VLM_BLEND_WEIGHT` can be chosen from
   data rather than left at its guessed 0.7.

`--cache` persists one record per frame. Re-analysis (`--analyse-only`) then needs no API call, no
model and no database, and an interrupted sweep resumes instead of re-paying. `--limit` defaults to
**25** so a first run is a smoke test rather than a surprise bill; `--limit 0` is all 340.

```bash
# Free end-to-end smoke first.
ollama pull qwen2.5vl:7b
.venv/Scripts/python.exe scripts/vlm_eval.py --model runs/pothole_v1/weights/best.onnx \
       --backend ollama --vlm-model qwen2.5vl:7b --limit 5

# Then a full sweep.
.venv/Scripts/python.exe scripts/vlm_eval.py --model runs/pothole_v1/weights/best.onnx \
       --backend openrouter --vlm-model <provider/model> \
       --limit 0 --cache runs/vlm-openrouter.json

# Re-analyse for free.
.venv/Scripts/python.exe scripts/vlm_eval.py --analyse-only --cache runs/vlm-openrouter.json
```

### One thing the instrument already measured

Over a 40-frame trial run, **Stage 1 produced a box on only 21 of 40 frames**. The rest reach the
verifier as *full frames*, because there is nothing to crop to. So `VLM_CROP_TO_DETECTIONS` only
bites on about half the corpus, and the crop-vs-full-frame ablation (`--no-crop`) is a question
about that half — not about the whole set. The script prints this split on every run.

### Results

**Run against a real model on 2026-09-03** — `qwen2.5vl:3b` via Ollama, all 340 labelled frames,
zero failures. Full write-up in [`phase-2.10-imagery-surfaces.md`](./phase-2.10-imagery-surfaces.md);
the headline is that **the verdict carries no usable signal**: recall 0.015 (1 of 65 potholes) and
precision 0.200 against a 0.191 base rate, a 1.05× lift. Its confidence is 0.8 or 0.9 on 96% of
frames, so it cannot be thresholded, and the blend's best F1 is identical (0.382) for every weight
from 0.00 to 0.70 — the VLM term is a near-constant offset. Its rationales negate this document's
own `VERIFY_PROMPT` definition rather than describing the image.

**§1's threshold findings are confirmed, and are not a VLM question.** The Stage-1 band table over
340 frames reproduces them exactly — 0.30–0.40 at 1.96× against the gray zone's 1.34×, and
0.75–1.01 holding one frame and zero potholes. `VLM_VERIFY_LOW`/`HIGH` are wrong for reasons that
do not depend on which model answers, so they stay uncalibrated pending a model whose verdict
separates anything.

**The measurement also found a crash in the production crop path** that no stub could: `_crop`
emitted crops below the vision encoder's patch factor and killed the model runner on 88 of 340
calls. Fixed with a `MIN_CROP_PX` floor; see 2.10.

The pre-existing stub verification stands as the instrument's own check: DB → ONNX → crop → HTTP →
parse → blend → report → cache, database confirmed unchanged, and the report correctly scored
hash-derived noise as noise (precision 0.333 against a 0.225 base rate).

## 4. Two constraints for whatever the measurement says

**Pseudo-labelling is gated on §3.1, and negatives are gated harder than positives.** Adding roughly
200 negatives per round is exactly what took recall 0.708 → 0.215 across v2, v3 and v4
([research record §4.1](../research/detection-research-record.md)). A VLM would generate them by the
thousand. A human-confirmed positive is the safe direction; a machine-generated negative is the
known failure mode at ten times the scale.

**Cloud verification is an outbound transfer of roadway imagery to a third party.** Frames are
EXIF-stripped on ingest (`strip_jpeg_metadata`), so location metadata is gone, but pixels can hold
plates and faces. Phase 4.3 commits to a GDPR/DPIA table and municipal deployment makes that
load-bearing. **Prefer `ollama` for anything beyond research**; cloud backends are a research
convenience, and the fact that both paths run the same client means switching is one env var.

## 5. Deliberately not done

- **Splitting VLM verification into its own pass.** Today the call is inline and serial inside the
  detection job: at `vlm_max_calls_per_run=50` and `vlm_timeout=30` a single run can occupy 25
  minutes of a 2-minute tick, and re-verifying with a better model means re-running YOLO over
  everything. The fix is a `vlm_verified_at` flag plus a separate job — with the VLM calls
  themselves orchestrated in n8n, which already handles retry, backoff and provider fallback, while
  Postgres keeps the claim and the write. Deferred until the component is worth building around.
- **Anomaly discovery** (open-vocabulary "what else is wrong with this road"). Wanted, but appending
  an open-ended question to `VERIFY_PROMPT` can degrade the binary verdict it shares a call with, so
  it belongs in a second prompt variant measured against the first — not folded into the baseline
  before the baseline exists.
- **Recalibrating `VLM_VERIFY_LOW`/`HIGH`.** §1 says what they must *not* be. What they should be
  depends on the blended band table, which needs §3 to run.
