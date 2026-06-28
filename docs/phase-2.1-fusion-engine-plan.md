# Phase 2.1 — Sensor Classification + Fusion Engine v1 (As-Built)

> Status: **Implemented.** Companion to [`docs/roadmap.md`](./roadmap.md) §2.4 and
> [`docs/enterprise-architecture-plan.md`](./enterprise-architecture-plan.md) §4.3. This is the
> ported, server-side realization of the original MATLAB accelerometer-classification research,
> feeding a sensor↔visual fusion engine.

## Context

The server was at **Phase 2.0** (ingestion: events, frames, health, rate limiting; generic `asset_*`
schema). The sensor and camera pipelines never read each other's results. The user's original 2017
MATLAB research (`C:\Users\satta\Desktop\MatlabCode`, 20 `.m` files) turned out to be an
**unsupervised accelerometer classifier** (Pothole / Crack / NotPothole), not a camera-fusion engine.
Phase 2.1 ports that method server-side and uses its `P(pothole)` as the sensor term of a sigmoid
fusion with the camera's on-device probability.

**No training files existed**, but the method is unsupervised and its features come from columns the
server already stores — so the model **self-bootstraps** from accumulated observations. The single
human step in the research (naming the 3 discovered clusters) is replaced by a deterministic
energy-rank rule grounded in the anomaly-detection normality assumption.

Two research-driven modernizations were folded in: the Hotelling-T² membership test was replaced by
an **Isolation Forest** outlier gate, and an **IRI-style severity** output was added.

## What the MATLAB code does → how it was ported

| MATLAB | Python | Note |
|---|---|---|
| `Rotation.m` | (n/a server-side) | orientation correction already done on-device |
| `finding.m` | `app/sensor_model/features.py` | `ratio = magnitude/accel_std`, `gbar_in_max` |
| `zscore` (many) | standardization stats in the model artifact | stored, reused at score time |
| `kmeansplusplus.m`, `Kmesnspls.m`, `GMM.m`, `gaussianmix.m` | `GaussianMixture(init_params='k-means++')` in `fit.py` | one call replaces all |
| `AIC_BIC.m`, `AICNoSamp.m` | `.bic()` recorded; fixed 3-class taxonomy | model selection / observability |
| `mle2.m`, `mLE.m`, `maxLike.m`, `loglikli.m` | `app/sensor_model/score.py` | scipy multivariate-Gaussian posterior → P(pothole) |
| `ClusterFinding.m`, `CheckIfClustered.m` | **IsolationForest** gate in `score.py` | modernized outlier rejection (replaces Hotelling-T²) |
| (new) IRI-style severity | `features.severity()` | label-free severity proxy |
| `clusbearing.m` | fit-trigger rationale + **Phase 2.2** | bearing segmentation |
| `DScan.m` | **Phase 2.2** (spatial DBSCAN) | not in the 2.1 serving path |
| `HighPass.m`, `FFT.m` | excluded (exploratory) | optional Phase 3 feature-eng |
| `ClusterCalc` (missing in the originals) | re-implemented in `fit.py` | the gap is filled |

## Implementation

### `app/sensor_model/` — the ported classifier
- `features.py` — `ratio`/`gbar` features (pure), `severity()` IRI-style proxy.
- `model.py` — `SensorModel` dataclass (standardization, components, class_map, severity calib,
  GMM + IsolationForest objects), `Standardization`, `SeverityCalibration`, the class taxonomy.
- `fit.py` — `fit_sensor_model()`: standardize → `GaussianMixture(k=3, init_params='k-means++',
  n_init=10, covariance_type='full', random_state)` → energy-rank component→class assignment →
  `IsolationForest` gate → joblib blob. Deterministic under fixed `random_state`.
- `score.py` — `score_observation()`: standardize → scipy multivariate-Gaussian class posteriors →
  `P(pothole)` + class; Isolation-Forest outlier flag; severity. Pure-math classification (no
  unpickle needed), so it's trivially unit-tested.
- `store.py` — `save_model()` / `load_active_model()` against the `sensor_model` table (one active
  row; refit = new version, rollback by flipping `is_active`).

### `app/fusion/` — pairing + fuse
- `engine.py` — `FusionEngine` Protocol + frozen `FusionInput`/`FusionOutput`; stable `sigmoid`/
  `logit`/`clamp01`. `runtime_ms` lives only in `debug` (never persisted).
- `matlab_port_v1.py` — primary engine: `fused = sigmoid(w_s·logit(P_sensor) + w_v·logit(P_visual))`;
  missing terms drop out; severity passed through from the sensor model.
- `python_v1.py` — heuristic cold-start fallback (derives a sensor probability from standardized
  ratio) used until a model is active.
- `registry.py` — `get_engine(model)`: primary when a model is active, else fallback.
- `service.py` — `run_fit_job()` (gated on accumulated observations) and `run_fusion_job()`
  (score unscored observations → pair unprocessed frames with the nearest same-device observation via
  `ST_DWithin` + `ROW_NUMBER` → fuse → upsert `fusion_pair` + write `fusion_run` → mark frames
  processed). Advisory-locked; pairing wrapped in one transaction.
- `scheduler.py` — `AsyncIOScheduler` with the fit + fusion jobs (`max_instances=1`, `coalesce`),
  started/stopped from the app lifespan, gated by `FUSION_ENABLED` / `SENSOR_FIT_ENABLED`.

### Schema — `migrations/002_sensor_model_and_fusion.sql` (additive, idempotent)
- `sensor_model` (versioned artifacts: standardization/components/class_map/severity JSONB, joblib
  `model_blob`, pinned `sklearn_version`, one active row via a partial unique index).
- `asset_observation`: `sensor_class`, `sensor_p_pothole`, `sensor_severity`, `sensor_is_outlier`,
  `sensor_model_version`, `scored_at` (+ unscored partial index).
- `fusion_pair`: `severity`.

### Config — `app/config.py`
Fusion (`fusion_enabled`, `fusion_engine_version`, interval, batch, window ms/m, `w_s`/`w_v`) and
sensor-model (`sensor_fit_*`, `sensor_iforest_contamination`, `sensor_random_state`, severity
calibration, heuristic fallback constants). Dependencies added: `APScheduler`, `numpy`, `scipy`,
`scikit-learn` (pinned), `joblib`.

### Determinism
Scoring/fusion is bit-stable (pure math over the frozen artifact + total pairing order). Fitting is a
discrete, versioned event with fixed `random_state`; each artifact is frozen once written. Refit = new
version.

## Fixed along the way
A real production bug in `app/services/frame_service.py` (the frame timestamp was passed to a
`timestamptz` parameter as a string, which asyncpg rejects) was fixed to match `event_service.py`.
The test `client` fixture now runs the app lifespan so the asyncpg pool initializes under httpx's
`ASGITransport`, and three pre-existing tests that mutated `os.environ` at runtime were corrected to
mutate the cached `settings` object.

## Verification (all green)
- Unit (no DB): `tests/test_features.py`, `tests/test_fusion_math.py`, `tests/test_score.py`,
  `tests/test_fit.py`.
- Integration (Postgres+PostGIS via `docker-compose`): `tests/test_fusion_db.py` — fit-gate +
  activation, nearest-observation pairing, no-candidate frames still marked processed, and
  idempotent/deterministic re-runs.
- Run: `docker compose up -d --wait`, then
  `DATABASE_URL="postgresql://pothole:pothole@localhost:5433/pothole_db" pytest -q`
  (54 passed). `ruff check app/ tests/` clean except two pre-existing unrelated warnings.

## Open items (non-blocking)
1. Confirm the component→class energy rule on the first real fit; expose a config override.
2. Tune `sensor_fit_min_observations`, `sensor_fit_k_max`, `sensor_iforest_contamination` on pilot data.
3. Whether `crack` folds into `pothole` for `sensor_confidence` downstream.
4. Severity: revisit the fuller double-integration IRI from the raw `[180,10]` window once validated.
5. `model_blob`: inline `bytea` (current) vs. object-storage ref at scale.
