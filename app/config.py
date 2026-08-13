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

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_events_per_hour: int = 100
    rate_limit_frames_per_hour: int = 100

    # ── Ingestion ─────────────────────────────────────────────────────────────
    max_batch_size: int = 100
    max_frame_size_bytes: int = 10_485_760  # 10 MB

    # ── Fusion engine (Phase 2.1) ───────────────────────────────────────────────
    fusion_enabled: bool = True
    fusion_engine_version: str = "fusion.matlab_port_v1"
    fusion_interval_minutes: int = 5
    fusion_batch_size: int = 500
    fusion_window_ms: int = 3000          # |T_frame - T_event| pairing window
    fusion_window_m: float = 25.0         # spatial pairing window (meters)
    fusion_w_s: float = 0.5               # sensor weight in sigmoid fusion
    fusion_w_v: float = 0.5               # visual weight in sigmoid fusion

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
    detection_disagreement_threshold: float = 0.3  # |device − server| above this → logged

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
    vlm_backend: str = "none"                      # "none" | "claude" | "gemini" | "local_http"
    vlm_model_id: str = ""                         # provider model id; backend defaults if empty
    vlm_api_key: str = ""                          # cloud API key (claude/gemini)
    vlm_http_url: str = ""                         # OpenAI-compatible endpoint (local_http)
    vlm_timeout: float = 30.0                      # per-call timeout (seconds)

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


settings = Settings()
