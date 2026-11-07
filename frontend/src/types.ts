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
  downgrade_reason?: string;
  effective_model?: string;
  in_flight_reserved_cost_usd?: number;
  uncertain_reserved_cost_usd?: number;
  committed_cost_usd?: number;
  cost_known?: boolean;
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

export interface ContainmentEval {
  raw?: Record<string, any>;
  checks: Record<string, boolean>;
  passed: boolean;
  failed_checks: string[];
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
  containment: ContainmentEval;
  memory_plan?: Record<string, any>;
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
  integrity?: string;
  correctness?: string;
  integrity_detail?: { recorded_in_ledger: string; actual: string; match: boolean };
  correctness_detail?: { expected: Record<string, any>; actual: Record<string, any> };
  agent_report?: Record<string, any> | null;
  detail?: string;
}

export interface EvidenceReport {
  session_id: string;
  task: string;
  verification: Record<string, string>;
  session_started: boolean;
  start_failure?: Record<string, any> | null;
  inputs: { verdict: string; files: Record<string, any> };
  resource_limits: { verdict: string; checks?: Record<string, boolean>; semantics?: Record<string, string> };
  cleanup: { verdict: string; checks?: Record<string, boolean> };
  inference_verification: Record<string, any>;
  chain_valid: boolean;
  chain_detail: string;
  artifact_verification: ArtifactVerification;
  containment: { verdict: string; passed: boolean; checks: Record<string, boolean>; failed_checks: string[] };
  termination: { verdict: string; checks?: Record<string, boolean>; recorded_verdict?: string; reason?: string };
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
  truncated_copy?: { kept_entries: number; dropped_entries: number; integrity_valid: boolean; completeness: string; detail: string };
  truncation_detected?: boolean;
  tamper_detected: boolean;
  note: string;
}
