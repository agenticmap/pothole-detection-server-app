"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration.

    Values are loaded from environment variables (case-insensitive) or a .env file
    located in the server root directory.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql://pothole:pothole@localhost:5432/pothole_db"
    database_use_pooler: bool = False
    database_min_connections: int = 5
    database_max_connections: int = 20

    # ── Storage ───────────────────────────────────────────────────────────────
    storage_backend: str = "local"  # "local" | "supabase"
    storage_local_path: str = "./storage/frames"
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_storage_bucket: str = "frames"

    # ── Server ────────────────────────────────────────────────────────────────
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    env: str = "development"
    log_level: str = "INFO"  # root logger level, applied in app/main.py

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    # Sized for a real collection drive, not a demo. A drive's worth of buffered
    # events/frames drains in one burst when the device rejoins Wi-Fi; the old
    # 100/hour ceiling 429'd mid-drain and the client retried the same rows
    # forever. Frames are gated on-device to ~1 per 4 s (900/hour worst case).
    rate_limit_events_per_hour: int = 5000
    rate_limit_frames_per_hour: int = 5000

    # ── Ingestion ─────────────────────────────────────────────────────────────
    max_batch_size: int = 100
    max_frame_size_bytes: int = 10_485_760  # 10 MB

    # ── Fusion engine (Phase 2.1) ───────────────────────────────────────────────
    fusion_enabled: bool = True
    fusion_engine_version: str = "fusion.matlab_port_v1"
    fusion_interval_minutes: int = 5
    fusion_batch_size: int = 500
    fusion_w_s: float = 0.5               # sensor weight in sigmoid fusion
    fusion_w_v: float = 0.5               # visual weight in sigmoid fusion

    # ── Pairing search (Phase 2.2d) ─────────────────────────────────────────────
    # The camera sees a pothole while it is still AHEAD of the vehicle; the
    # accelerometer fires when the wheel arrives. So a correct pair has a non-zero
    # separation (the lead) and a NEGATIVE delta_ms (frame before event) — not the
    # delta=0/dist=0 that the pre-2.2d ranking preferred. Re-ranking the existing
    # candidates in pothole_db changed the winner for 713 of 2197 frames (32.5%).
    # See docs/phases/phase-2.2d-pairing-search.md.
    # false restores the pre-2.2d RANKING, not the pre-2.2d windows: it still uses
    # fusion_window_m and a fixed fusion_window_ms_max, because fusion_window_ms no
    # longer exists. For a byte-exact revert also set FUSION_WINDOW_M=25 and
    # FUSION_WINDOW_MS_MAX=3000, which were the old defaults.
    fusion_pairing_cost_enabled: bool = True
    # Ground-distance band the camera can usefully resolve a pothole in. A property
    # of the lens and the mount pitch, so THESE DEFAULTS ARE A HYPOTHESIS, NOT A
    # FINDING — only 3 potholes in pothole_db have a paired frame, far too few to
    # fit a band from. `scripts/pairing_eval.py --fit-lead` replaces them once
    # Phase 2.7's frame labels exist.
    fusion_lead_near_m: float = 5.0
    fusion_lead_far_m: float = 30.0
    # Divisor floor when converting a lead distance to an expected time offset, so
    # a stopped or speed-less observation does not blow up the kinematic term.
    fusion_speed_floor_mps: float = 5.0
    # Spatial window. Must span the lead band plus GPS error, or the search cannot
    # reach the frames that actually saw the pothole. Raised 25 → 40 in 2.2d: 25 m
    # reached 1842 frames, 40 m reaches 2197 (+19.3%).
    fusion_window_m: float = 40.0
    # Temporal window is DERIVED from speed — the two gates are one constraint, not
    # two. At 13.02 m/s (the measured median) 3000 ms of travel is 39 m, and 75 m at
    # p90, so the old fixed 3000 ms contradicted the 25 m spatial gate: above
    # ~8.3 m/s the spatial gate bound and the temporal one was dead weight. This is
    # now only the CEILING, for the low-speed case where the derived window opens up.
    fusion_window_ms_max: int = 8000
    # Cost weights. lead_penalty is in metres outside the band, kinematic in seconds
    # of residual, so the two are commensurate only by choice of these weights.
    fusion_w_lead: float = 1.0
    fusion_w_kinematic: float = 1.0
    # Added to a candidate whose frame was taken AFTER the event fired. Such a frame
    # is looking at road the car has already crossed, so it is admissible (GPS and
    # clock noise straddle zero) but should lose to any backward candidate.
    fusion_forward_penalty: float = 2.0
    # Frames are marked processed whether or not they paired, so an event that
    # uploads after its frame is a permanently missed pair. Observations lag frames
    # (median upload lag 3.2 h vs 1.7 h) and 450 candidate pairs in pothole_db have
    # the event arriving later — though 0 frames were actually lost, because the
    # 5-minute cadence absorbed it. This bounds the exposure regardless.
    fusion_retry_grace_minutes: int = 30

    # Frame-only cluster members: a frame that sees a pothole nobody drove over.
    # 98.6% of pothole observations have no coincident frame, so this is the largest
    # recall ceiling in the pipeline — and it CANNOT BE HONESTLY ENABLED YET.
    # server_probability is NULL on all 2916 frames (no model), leaving only the
    # on-device probability, whose confidence floor was lowered to ~5% mid-collection
    # (p50 0.118). Turning this on today would flood clustering with on-device
    # guesses. The gate opens once Phase 2.7 measures a threshold.
    fusion_frame_only_enabled: bool = False
    fusion_frame_only_min_probability: float = 0.5

    # ── Sensor model (unsupervised classifier, ported from MATLAB) ──────────────
    sensor_fit_enabled: bool = True
    sensor_fit_interval_minutes: int = 60
    sensor_fit_min_observations: int = 200   # N_min — gate before first fit
    sensor_fit_k_max: int = 5                # BIC sweep upper bound
    sensor_fit_k_default: int = 3            # pot / crack / not
    sensor_segment_min_points: int = 9       # clusbearing.m segment-close trigger
    sensor_bearing_change_deg: float = 45.0  # heading change that closes a segment
    sensor_iforest_contamination: float = 0.1  # IsolationForest gate sensitivity
    sensor_random_state: int = 42            # determinism for fit (GMM + IForest)

    # Severity (IRI-style proxy): severity = clamp(scale * magnitude / max(speed, speed_ref), 0, 1)
    severity_speed_ref: float = 5.0          # m/s floor to avoid divide-by-zero at low speed
    severity_scale: float = 2.0

    # Cold-start heuristic fallback (used until an active model exists)
    fallback_ratio_mean: float = 3.0
    fallback_ratio_std: float = 1.0

    # ── Clustering job (Phase 2.2) ──────────────────────────────────────────────
    clustering_enabled: bool = True
    clustering_interval_minutes: int = 15          # roadmap §2.5 cadence
    cluster_eps_m: float = 25.0                    # ST_ClusterDBSCAN radius (meters)
    cluster_min_points: int = 3                    # ST_ClusterDBSCAN core min
    cluster_window_days: int = 30                  # only members seen in last N days
    cluster_member_min_confidence: float = 0.5     # fused_confidence floor for pair members
    cluster_min_distinct_devices: int = 2          # below this → not public (read-path filter)

    # ── Spatiotemporal crowd fusion (Phase 2.2c) ────────────────────────────────
    # The integration half of Sattar's probabilistic crowdsourcing technique: cluster
    # confidence becomes a spatiotemporally weighted combination of its members'
    # class distributions instead of a plain mean, so a detection on the centroid
    # detected today outweighs one 20 m away from three weeks ago.
    # Falls back to the legacy mean per-cluster when members predate the
    # sensor_class_probs column, so it is safe to enable on un-rescored data.
    cluster_spatiotemporal_enabled: bool = True
    # RBF sigma floors. gamma = 1/(2*sigma^2) and sigma comes from the cluster's own
    # spread, so a cluster whose members sit at near-identical distances or times
    # would otherwise divide by zero. Also covers the single-member case.
    cluster_rbf_sigma_floor_m: float = 1.0
    cluster_rbf_sigma_floor_seconds: float = 3600.0
    # Symmetric Dirichlet prior, an extension BEYOND the paper. 0.0 reproduces its
    # result exactly. Above zero, small clusters are shrunk toward uniform and only
    # approach their observed value as corroborating members accumulate — i.e. it is
    # what makes three agreeing devices outrank one.
    cluster_prior_concentration: float = 0.0
    # Cluster identity (paper §4.4). A new group only merges into an existing cluster
    # when their headings agree, so opposing carriageways stay separate defects.
    # Android's bearing accuracy is not on the wire yet, so this is a fixed tolerance
    # rather than the paper's ±2σ; see docs/research/app-capture-findings.md F3.
    cluster_bearing_tolerance_deg: float = 45.0
    cluster_bearing_aware: bool = True

    # ── Operator dashboard frontend (Phase 2.5) ─────────────────────────────────
    # Built bundle, mounted at /dashboard when present. Relative paths resolve
    # against the repo root, not the CWD — uvicorn is not always launched from it.
    dashboard_dist_path: str = "dashboard/dist"
    # Protomaps PMTiles archive for the map background. A build artefact, not
    # source — see dashboard/README.md for how to regenerate it.
    basemap_path: str = "storage/basemap"

    # ── Operator dashboard vector tiles (Phase 2.5) ─────────────────────────────
    # At or below this zoom a tile is grid-aggregated. Measured on 20k synthetic
    # clusters over one city: an unaggregated z10 tile returned all 20k features in
    # 425 ms (over the 250 ms p95 ceiling); the same tile aggregated took 89 ms.
    tile_aggregate_max_zoom: int = 12
    # Raw observation points are only meaningful street-level, and an unbounded
    # low-zoom request would scan the largest table in the schema.
    tile_observations_min_zoom: int = 15
    tile_max_features: int = 4000              # per-tile cap; bounds payload + encode time
    tile_extent: int = 4096                    # MVT coordinate space (the de-facto standard)
    tile_buffer: int = 64                      # px of bleed so edge symbols render whole
    tile_aggregate_bins: int = 32              # grid cells across a tile when aggregating
    # Tile queries share the asyncpg pool with ingestion, and MapLibre fans out 4-8
    # requests per pan. Without a ceiling a slow tile starves POST /api/v1/events.
    tile_max_concurrency: int = 6
    tile_query_timeout_seconds: float = 2.0    # fail fast rather than hold a connection
    tile_cache_seconds: int = 60               # Cache-Control max-age (private: authenticated)

    # ── Server-side detection model (Phase 2.3) ─────────────────────────────────
    detection_enabled: bool = False                # gated off until a model is configured
    detection_backend: str = "none"                # "none" | "onnx" | "http" | "hybrid"
    detection_interval_minutes: int = 2            # poll cadence (shorter than fusion's 5)
    detection_batch_size: int = 200
    detection_model_path: str = ""                 # path to the YOLOv8 .onnx (backend=onnx)
    detection_model_id: str = "yolov8s_pothole_v1"
    detection_http_url: str = ""                   # external inference endpoint (backend=http)
    detection_input_size: int = 640
    detection_conf_threshold: float = 0.25
    detection_iou_threshold: float = 0.45          # NMS overlap ceiling (backend=onnx)
    detection_disagreement_threshold: float = 0.3  # |device − server| above this → logged
    detection_http_timeout: float = 30.0           # per-frame timeout (backend=http)

    # Class map (Phase 2.7b). Comma-separated; position IS the class_id, so order
    # must match the model's data.yaml `names:` exactly. Only the primary class may
    # set server_probability: fusion blends that number with no notion of class, so
    # a confident manhole reaching it would be read as a confirmed pothole.
    detection_class_names: str = "pothole"
    detection_primary_class_id: int = 0

    # ROI crop (Phase 2.7). Uploaded frames are 480x640 portrait windshield shots:
    # sky and trees fill the top half and the hood the bottom ~15%, so letterboxing
    # the whole frame spends most of the 640px budget where a pothole cannot be.
    # Fractions of frame HEIGHT; full width is always kept. Boxes come back in
    # full-frame coordinates either way, so this is safe to flip.
    detection_roi_enabled: bool = True
    detection_roi_top: float = 0.45                # below the horizon
    detection_roi_bottom: float = 0.90             # above the hood

    # ── Hybrid detector (Phase 2.3b — YOLO Stage 1 + VLM verifier) ──────────────
    # backend="hybrid" runs a Stage-1 detector (onnx/http) and, only for frames
    # whose Stage-1 probability lands in the gray zone, asks a VLM to confirm the
    # detection and reject look-alikes (shadows, manholes, wet patches, markings).
    detection_hybrid_stage1: str = "onnx"          # which detector Stage 1 uses: "onnx" | "http"
    vlm_verify_low: float = 0.40                   # gray-zone lower bound (below → trust Stage 1)
    vlm_verify_high: float = 0.75                  # gray-zone upper bound (above → trust Stage 1)
    vlm_crop_to_detections: bool = True            # crop to the union bbox before the VLM call
    vlm_crop_margin: float = 0.20                  # expand the crop by this fraction on each side
    vlm_blend_weight: float = 0.7                  # VLM weight in the logit-space blend (0..1)
    vlm_max_calls_per_run: int = 50                # per-detection-run cap on VLM calls (cost bound)

    # ── Pluggable VLM verifier (provider-agnostic: cloud + local) ───────────────
    # openrouter/ollama/local_http all speak the same OpenAI-compatible wire format
    # and share one client; they differ only in the default URL and whether a key is
    # required. vlm_http_url overrides the default for any of them.
    # "none"|"claude"|"gemini"|"openrouter"|"ollama"|"local_http"
    vlm_backend: str = "none"
    vlm_model_id: str = ""                         # provider model id; backend defaults if empty
    vlm_api_key: str = ""                          # cloud API key (claude/gemini/openrouter)
    vlm_http_url: str = ""                         # overrides the backend's default endpoint
    vlm_timeout: float = 30.0                      # per-call timeout (seconds)
    vlm_json_mode: bool = True                     # response_format=json_object; off if rejected
    vlm_http_referer: str = ""                     # OpenRouter attribution header (HTTP-Referer)
    vlm_http_title: str = ""                       # OpenRouter attribution header (X-Title)

    # ── Auth — city-staff tier (Phase 2.4) ──────────────────────────────────────
    # The anonymous device tier is unaffected by any of these. They gate ONLY the
    # staff endpoints (/auth/*, /potholes/detail).
    auth_enabled: bool = True
    # RS256 keypair (PEM). Asymmetric on purpose: the public key is published at
    # /.well-known/jwks.json so the issuer can later be swapped for OIDC/SSO
    # without changing the API's validation path. If the private key is empty in
    # development, an EPHEMERAL keypair is generated at startup (tokens won't
    # survive a restart — fine for local dev, never for production).
    auth_jwt_private_key_pem: str = ""
    auth_jwt_public_key_pem: str = ""          # derived from the private key if empty
    auth_jwt_kid: str = "auth-key-1"           # key id placed in the JWT header + JWKS
    auth_jwt_issuer: str = "pothole-detection-server"   # `iss` claim, validated on decode
    auth_access_token_ttl_minutes: int = 30    # short-lived access token
    auth_refresh_token_ttl_days: int = 30      # rotating refresh token lifetime

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def detection_class_name_list(self) -> list[str]:
        """Parse comma-separated detection class names into a list. Index = class_id."""
        return [c.strip() for c in self.detection_class_names.split(",") if c.strip()]


settings = Settings()
