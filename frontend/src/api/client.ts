import type { EvidenceReport, SessionSnapshot, TamperResult, Termination } from "../types";

const API_BASE = "";

// View mode: "operator" uses the logged-in /sessions API; "public" uses /public/sessions
// (a published replay, or a guest run identified by its scoped guest token).
let view: { public: boolean; guestToken?: string | null } = { public: false };
export function setViewMode(v: { public: boolean; guestToken?: string | null }) { view = v; }
export function isPublicView() { return view.public; }
const sbase = (id: string) => (view.public ? `${API_BASE}/public/sessions/${id}` : `${API_BASE}/sessions/${id}`);
const hdrs = (): Record<string, string> => (view.public && view.guestToken ? { "X-Guest-Token": view.guestToken } : {});
const req = (url: string, init: RequestInit = {}) => fetch(url, { ...init, headers: { ...(init.headers || {}), ...hdrs() } });

async function errText(r: Response): Promise<string> {
  try {
    const j = await r.json();
    return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
  } catch {
    return `${r.status}`;
  }
}

export interface PublicScenario { id: string; title: string; plain: string; expect: string; expect_color: string;
  task: string; cpu: number; mem: number; tokens: number; wall: number }
export async function getScenarios(): Promise<{ scenarios: PublicScenario[]; guest_mode: boolean }> {
  return (await fetch(`${API_BASE}/public/scenarios`)).json();
}
export async function getReplays(): Promise<any[]> {
  const r = await fetch(`${API_BASE}/public/replays`);
  return r.ok ? r.json() : [];
}
export async function tryScenario(scenario: string): Promise<{ session_id: string; guest_token: string }> {
  const r = await fetch(`${API_BASE}/public/try`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario }) });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}
export async function setFeatured(sessionId: string, on: boolean): Promise<{ featured: boolean }> {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/feature?on=${on}`, { method: "POST" });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}
export async function getFeatured(sessionId: string): Promise<{ featured: boolean }> {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/feature`);
  return r.ok ? r.json() : { featured: false };
}

export async function createSession(body: {
  task: string;
  cpu_budget_seconds?: number;
  memory_budget_mb?: number;
  wall_clock_seconds?: number;
  token_budget?: number;
  cost_limit_usd?: number;
}): Promise<{ session_id: string; status: string; runtime_type: string; containment: any }> {
  const r = await fetch(`${API_BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      const j = await r.json();
      const d = j.detail;
      msg = typeof d === "string" ? d : `${d.category}: ${d.detail} (session ${d.session_id} — evidence recorded)`;
    } catch {
      /* noop */
    }
    throw new Error(`Session not started: ${msg}`);
  }
  return r.json();
}

export async function getSession(sessionId: string): Promise<SessionSnapshot> {
  const r = await req(sbase(sessionId));
  if (!r.ok) throw new Error(`getSession failed: ${r.status}`);
  return r.json();
}

export async function getEvidence(sessionId: string): Promise<EvidenceReport> {
  const r = await req(`${sbase(sessionId)}/evidence`);
  if (!r.ok) throw new Error(`getEvidence failed: ${r.status}`);
  return r.json();
}

export async function pauseSession(
  sessionId: string
): Promise<{ paused: boolean; state: string }> {
  const r = await req(`${sbase(sessionId)}/pause`, { method: "POST" });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

export async function resumeSession(
  sessionId: string,
  extraCpuSeconds = 0
): Promise<{ resumed: boolean; state: string; new_cpu_budget_usec: number }> {
  const r = await req(
    view.public ? `${sbase(sessionId)}/resume` : `${sbase(sessionId)}/resume?extra_cpu_seconds=${extraCpuSeconds}`,
    { method: "POST" }
  );
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

export async function killSession(
  sessionId: string
): Promise<{ killed: boolean; verdict: Termination["verdict"]; checks: Record<string, boolean>; state: string; workload_gone: boolean }> {
  const r = await req(`${sbase(sessionId)}/kill`, { method: "POST" });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

export async function getEvents(sessionId: string): Promise<any[]> {
  const r = await req(`${sbase(sessionId)}/events`);
  if (!r.ok) throw new Error(`getEvents failed: ${r.status}`);
  return r.json();
}

export async function tamperTest(sessionId: string): Promise<TamperResult> {
  const r = await req(`${sbase(sessionId)}/evidence/tamper-test`, { method: "POST" });
  if (!r.ok) throw new Error(`tamperTest failed: ${r.status}`);
  return r.json();
}

export const exportUrl = (sessionId: string) =>
  `${sbase(sessionId)}/evidence/export${view.public && view.guestToken ? `?g=${view.guestToken}` : ""}`;

export async function authStatus(): Promise<{ authenticated: boolean; configured: boolean }> {
  const r = await fetch(`${API_BASE}/auth/status`);
  return r.json();
}

export async function login(token: string): Promise<void> {
  const r = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!r.ok) throw new Error(r.status === 429 ? "Too many attempts — wait a minute" : "Invalid access code");
}

export async function getHealth(): Promise<any> {
  const r = await fetch(`${API_BASE}/health`);
  return r.json();
}

export interface StreamHandlers {
  onState: (e: any) => void;
  onSample: (e: any) => void;
  onRecommend: (e: any) => void;
  onLedger: (e: any) => void;
  onDone: (e: any) => void;
  onError: (e: Event) => void;
}

export function streamSession(sessionId: string, h: StreamHandlers): () => void {
  const es = new EventSource(`${API_BASE}/sessions/${sessionId}/stream`);
  es.addEventListener("state", (e) => h.onState(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("sample", (e) => h.onSample(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("recommend", (e) => h.onRecommend(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("ledger", (e) => h.onLedger(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", (e) => h.onDone(JSON.parse((e as MessageEvent).data)));
  es.onerror = h.onError;
  return () => es.close();
}