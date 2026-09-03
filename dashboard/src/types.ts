/** Hand-written mirrors of app/models/clusters.py. Keep field names exact. */

export interface ClusterMemberItem {
  client_id: string;
  lat: number;
  lon: number;
  ts: string | null;
  /** Per-cluster ordinal ("A", "B"). NOT stable across clusters — never key on it. */
  device_ref: string;
  speed_mps: number | null;
  accuracy_m: number | null;
  sensor_class: string | null;
  sensor_p_pothole: number | null;
  sensor_severity: number | null;
  sensor_is_outlier: boolean | null;
  fused_confidence: number | null;
}

/**
 * One detected box. Normalized 0..1, corner-origin, FULL-frame.
 *
 * The ROI crop and letterbox padding are already undone server-side
 * (app/detection/onnx_v1.py::_to_detection), so rendering is `x * renderedWidth`
 * with no geometry knowledge here. Same convention as the human boxes in frame_box.
 */
export interface DetectionBox {
  x: number;
  y: number;
  w: number;
  h: number;
  confidence: number;
  label?: string | null;
  class_id?: number | null;
}

/**
 * The hybrid backend's VLM verdict, lifted out of server_detections server-side.
 * Present only when a VLM verified the frame — under that backend server_probability
 * is a blend, and this is what tells the two apart.
 *
 * `rationale` is free text from a third-party model. Render with textContent only;
 * dom.ts has no innerHTML escape hatch, so use el({ text }) and never build markup.
 */
export interface VlmVerdict {
  is_pothole: boolean;
  confidence: number;
  severity: string | null;
  rationale: string;
  model_id: string;
}

export interface ClusterFrameItem {
  client_id: string;
  lat: number;
  lon: number;
  ts: string | null;
  /** Root-relative API path — use verbatim, do not prefix with a base. */
  image_url: string;
  device_probability: number | null;
  server_probability: number | null;
  server_model_id: string | null;
  /** null means "not yet scored", which is distinct from a score of 0. */
  detected_at: string | null;
  paired_observation_id: string;
  fused_confidence: number | null;
  delta_ms: number | null;
  delta_m: number | null;
  /** Server detector boxes. Never contains the VLM verdict — filtered server-side. */
  server_boxes: DetectionBox[];
  /** On-device detector boxes, same convention. */
  device_boxes: DetectionBox[];
  vlm_verdict: VlmVerdict | null;
}

/**
 * One frame on its own — GET /api/v1/frames/{client_id}.
 *
 * Mirrors app/models/clusters.py::FrameDetailResponse. Nearly ClusterFrameItem, with
 * `paired_observation_id` NULLABLE: the map's frames layer includes frames that never
 * paired, and those are often the interesting ones — a frame the detector scored highly
 * that matched no sensor event contributed to no cluster.
 *
 * Satisfies `FrameEvidence` structurally, so it works with the existing scoreLines /
 * overlayBoxesFor / vlmSummary helpers unchanged.
 */
export interface FrameDetail extends Omit<ClusterFrameItem, 'paired_observation_id'> {
  paired_observation_id: string | null;
}

export interface RepairLogItem {
  repair_id: string;
  action: 'repaired' | 'unrepaired' | string;
  note: string | null;
  user_id: string;
  user_email: string | null;
  at: string;
}

export interface ClusterDetailResponse {
  cluster_id: string;
  asset_type: string;
  lat: number;
  lon: number;
  severity: number | null;
  confidence: number | null;
  observation_count: number;
  distinct_devices: number;
  /**
   * Distinct (device, drive) passes — the paper's unit of corroboration.
   *
   * Non-optional: these were declared optional here while the server never sent
   * them, so `?? 0` in the panel silently rendered "0 passes" for every cluster.
   * Requiring them means a future server that drops them fails to typecheck.
   */
  distinct_passes: number;
  /** Seconds between earliest and latest member; seconds means one drive-past. */
  member_span_s: number | null;
  last_seen: string | null;
  source: string | null;
  repaired_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  members: ClusterMemberItem[];
  members_truncated: boolean;
  frames: ClusterFrameItem[];
  frames_truncated: boolean;
  repair_history: RepairLogItem[];
  generated_at: string;
}

export interface RepairResponse {
  cluster_id: string;
  repaired_at: string | null;
  changed: boolean;
  repair_id: string | null;
}
