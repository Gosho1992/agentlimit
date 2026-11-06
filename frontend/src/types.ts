export type GovernanceState = "NORMAL" | "ALERT" | "RECOMMEND" | "THROTTLE" | "PAUSE" | "KILL";

export interface LedgerEntry {
  event_type: string;
  session_id: string;
  timestamp_utc: string;
  prev_hash: string;
  payload: Record<string, any>;
  entry_hash: string;
  signature: string;
}

export interface BudgetSnapshot {
  policy_id?: string;
  cpu_budget_usec: number;
  memory_limit_bytes: number;
  wall_clock_limit_seconds: number;
  token_limit: number;
  cost_limit_usd?: number;
  alert_pct: number;
  recommend_pct: number;
  throttle_pct: number;
  pause_pct: number;
}

export interface InferenceSnapshot {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  token_limit?: number;
  estimated_cost_usd: number;
  cost_usd: number;
  cost_limit_usd?: number;
  calls?: number;
  refused_calls?: number;
  throttled: boolean;
  revoked?: boolean;
  model_downgrade_available?: boolean;
  cheap_model?: string | null;
  in_flight?: number;
  uncertain_requests?: number;
  uncertain_reserved_tokens?: number;
  last_model: string;
}

export interface Termination {
  verdict: "KILL_CONFIRMED" | "KILL_FAILED";
  reason: string;
  checks: Record<string, boolean>;
  sandbox_pid?: number;
  final_cpu_usec?: number;
  final_tokens?: number;
}

export interface Containment {
  passed?: boolean;
  egress_blocked?: boolean;
  dns_blocked?: boolean;
  no_secrets_in_env?: boolean;
  secret_env_vars?: string[];
  host_paths_visible?: string[];
  workspace_files?: string[];
  probe_error?: string;
  uid?: number;
  inputs_read_only?: boolean;
}

export interface Recommendation {
  recommendation_id?: string;
  rule_id: string;
  severity: string;
  metric: string;
  observed: number;
  threshold: number;
  unit?: string;
  suggestion: string;
  action: string;
  auto_applicable: boolean;
  risk?: string;
  status?: string;
  state_at_issue?: string;
  evidence?: Record<string, any>;
}

export interface SessionSnapshot {
  session_id: string;
  task: string;
  state: GovernanceState;
  agent_status: string;
  steps: number;
  cpu_usec: number;
  memory_bytes: number;
  memory_peak_bytes: number;
  tokens: number;
  wall_seconds: number;
  utilisation_pct: Record<string, number>;
  runtime_type: string;
  sandbox_pid?: number;
  memory_telemetry_valid: boolean;
  inference: InferenceSnapshot;
  containment: Containment;
  termination: Termination | null;
  lifecycle: Record<string, any> | null;
  recommendations: Recommendation[];
  agent_claim: string | null;
  budget: BudgetSnapshot;
  archived?: boolean;
}

export interface ArtifactVerification {
  level: number;
  verdict: "VERIFIED" | "INTEGRITY_ONLY" | "FAILED" | "NO_ARTIFACT";
  inputs?: { verified: boolean; files: Record<string, { recorded: string; actual: string; match: boolean }> };
  integrity?: { recorded_in_ledger: string; actual: string; match: boolean };
  correctness?: { expected: Record<string, any>; actual: Record<string, any> };
  agent_report?: Record<string, any> | null;
  detail?: string;
}

export interface EvidenceReport {
  session_id: string;
  task: string;
  chain_valid: boolean;
  chain_detail: string;
  artifact_verification: ArtifactVerification;
  containment: Record<string, { ok: boolean; detail: any }>;
  termination: Termination | null;
  lifecycle: Record<string, any> | null;
  public_key_b64: string;
  public_key_fingerprint?: string;
  archived?: boolean;
  final_state: GovernanceState;
  agent_status: string;
  steps_completed: number;
  inference: InferenceSnapshot;
  entry_count: number;
  entries: LedgerEntry[];
}

export interface TamperResult {
  original: { valid: boolean; detail: string };
  tampered_copy: { entry_index: number; field: string; before: any; after: any; valid: boolean; detail: string };
  tamper_detected: boolean;
  note: string;
}
