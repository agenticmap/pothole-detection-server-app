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
  /** Distinct (device, drive) passes — the paper's unit of corroboration. */
  distinct_passes?: number;
  /** Seconds between earliest and latest member; seconds means one drive-past. */
  member_span_s?: number | null;
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
