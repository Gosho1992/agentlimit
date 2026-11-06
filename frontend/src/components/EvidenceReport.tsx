import { useEffect, useState } from "react";
import { exportUrl, getEvidence, tamperTest } from "../api/client";
import type { EvidenceReport as Report, TamperResult } from "../types";

const LABEL: Record<string, string> = {
  process_isolation: "Process isolation",
  secret_hygiene: "Secret hygiene",
  resource_limits: "Resource limits",
  lifecycle_discipline: "Lifecycle discipline",
};

export default function EvidenceReport({ sessionId, autoLoad = false }: { sessionId: string; autoLoad?: boolean }) {
  const [report, setReport] = useState<Report | null>(null);
  const [tamper, setTamper] = useState<TamperResult | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      setReport(await getEvidence(sessionId));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (autoLoad) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoLoad]);

  const a = report?.artifact_verification;
  const verdictCls = a?.verdict === "VERIFIED" ? "ok" : a?.verdict === "FAILED" ? "fail" : "muted";

  return (
    <div className="evidence">
      <div className="evidence-actions">
        <button onClick={load} disabled={loading}>{loading ? "Verifying…" : report ? "Re-verify" : "Load & verify evidence"}</button>
        <a className="button" href={exportUrl(sessionId)}>Download signed ledger (.jsonl)</a>
        <button onClick={async () => setTamper(await tamperTest(sessionId))}>Run tamper test</button>
      </div>

{tamper && (
        <div className={`tamper ${tamper.tamper_detected ? "ok" : "fail"}`}>
          <strong>{tamper.tamper_detected ? "✓ Tampering detected" : "✗ Tampering NOT detected"}</strong>
          <p className="small">
            Changed {tamper.tampered_copy.field} at entry {tamper.tampered_copy.entry_index}:{" "}
            {String(tamper.tampered_copy.before)} → {String(tamper.tampered_copy.after)} in a copy. Verifier:{" "}
            {tamper.tampered_copy.detail}. Original ledger: {tamper.original.valid ? "valid" : "INVALID"}.
          </p>
          <p className="muted small">{tamper.note}</p>
        </div>
      )}

      {report && (
        <div className="evidence-grid">
          <div>
            <h4>What the agent claimed vs what was verified</h4>
            <table className="claims">
              <thead>
                <tr><th></th><th>Agent's output</th><th>Independent recomputation</th></tr>
              </thead>
              <tbody>
                {a?.correctness ? (
                  Object.keys(a.correctness.expected).map((k) => (
                    <tr key={k} className={a.correctness!.expected[k] === a.correctness!.actual[k] ? "ok" : "fail"}>
                      <td>{k}</td>
                      <td>{String(a.correctness!.actual[k])}</td>
                      <td>{String(a.correctness!.expected[k])}</td>
                    </tr>
                  ))
                ) : (
                  <tr><td colSpan={3} className="muted">{a?.detail}</td></tr>
                )}
              </tbody>
            </table>
            <p>
              <strong>Artifact verdict (Level {a?.level}):</strong> <span className={verdictCls}>{a?.verdict}</span>
            </p>
            {a?.integrity && (
              <p className="muted small">
                output sha256 recorded in ledger {a.integrity.recorded_in_ledger?.slice(0, 16)}… · file on disk{" "}
                {a.integrity.actual?.slice(0, 16)}… · {a.integrity.match ? "match" : "MISMATCH"}
              </p>
            )}
            {a?.inputs && (
              <p className="muted small">
                inputs re-hashed against session-start snapshot: {a.inputs.verified ? "match" : "MISMATCH"} (
                {Object.keys(a.inputs.files).join(", ")})
              </p>
            )}
          </div>

          <div>
            <h4>Ledger</h4>
            <p>
              <strong>Hash chain + Ed25519 signatures:</strong>{" "}
              <span className={report.chain_valid ? "ok" : "fail"}>{report.chain_valid ? "VALID" : "INVALID"}</span>{" "}
              <span className="muted">({report.chain_detail})</span>
            </p>
            <p className="small">
              key fingerprint <code>{report.public_key_fingerprint}</code>{" "}
              <span className="muted">— compare with the fingerprint published in the README/submission</span>
            </p>
            <p className="muted small">
              public key <code>{report.public_key_b64}</code>
            </p>
            <p className="muted small">
              Verify offline without trusting this server:{" "}
              <code>python backend/scripts/verify_ledger.py ledger.jsonl {report.public_key_b64.slice(0, 10)}…</code>
            </p>
          </div>

          <div>
            <h4>Blast Radius Zero containment requirements</h4>
            <ul className="checklist">
              {Object.entries(report.containment).map(([k, v]) => (
                <li key={k} className={v.ok ? "ok" : "fail"} title={typeof v.detail === "string" ? v.detail : JSON.stringify(v.detail)}>
                  {v.ok ? "✓" : "✗"} {LABEL[k] ?? k}
                  <div className="muted small">
                    {typeof v.detail === "string"
                      ? v.detail
                      : Object.entries(v.detail || {}).map(([dk, dv]) => `${dk}=${dv}`).join(" · ")}
                  </div>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <h4>Session</h4>
            <p className="small">
              final state <span className={`state-pill state-${report.final_state.toLowerCase()}`}>{report.final_state}</span> · agent{" "}
              {report.agent_status} · {report.steps_completed} steps · {report.inference.total_tokens.toLocaleString()} tokens ·
              est. ${report.inference.estimated_cost_usd.toFixed(5)} · {report.entry_count} signed entries
            </p>
            {report.termination && (
              <p className="small">
                termination: <span className={report.termination.verdict === "KILL_CONFIRMED" ? "ok" : "fail"}>{report.termination.verdict}</span>
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
