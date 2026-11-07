import { useEffect, useState } from "react";
import { exportUrl, getEvidence, tamperTest } from "../api/client";
import type { EvidenceReport as Report, TamperResult } from "../types";
import { PUBLISHED_FINGERPRINT, VERDICT_PLAIN, VERIFY_PLAIN } from "../plain";
import Term from "./Term";

const AREA: Record<string, string> = {
  ledger_integrity: "Ledger integrity (hash chain + Ed25519)",
  ledger_completeness: "Ledger completeness (ends with signed terminal event)",
  input_integrity: "Input integrity (snapshot re-hashed)",
  artifact_integrity: "Artifact integrity (output re-hashed)",
  artifact_correctness: "Artifact correctness (independent recomputation)",
  containment: "Containment (re-evaluated from signed probe)",
  resource_limits: "Resource limits (kernel readback vs budget)",
  termination: "Termination (all kill checks)",
  execution_cleanup: "Execution cleanup",
  inference_accounting: "Inference accounting",
};

const VCLS: Record<string, string> = { VALID: "ok", FAILED: "fail", PARTIAL: "warn", PENDING: "muted", NOT_APPLICABLE: "muted",
  COMPLETE: "ok", IN_PROGRESS: "muted", INTERRUPTED: "warn", INCOMPLETE: "fail" };

export default function EvidenceReport({ sessionId, autoLoad = false, technical = false }: { sessionId: string; autoLoad?: boolean; technical?: boolean }) {
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
        <button onClick={load} disabled={loading}>{loading ? "Checking…" : report ? "Check again" : "Check the proof"}</button>
        <button id="tour-tamper" className="btn-cheat" onClick={async () => setTamper(await tamperTest(sessionId))}>
          🕵️ Try to cheat (tamper test)
        </button>
        <a className="button" href={exportUrl(sessionId)}>⬇ Download the signed record</a>
      </div>

      {tamper && (
        <div className="cheat">
          <div className={`cheat-row ${tamper.tamper_detected ? "caught" : "missed"}`}>
            <span className="cheat-icon">{tamper.tamper_detected ? "🚨" : "✗"}</span>
            <div>
              <strong>{tamper.tamper_detected ? "Caught! Changing the record is detected." : "Not detected"}</strong>
              <p className="small">
                On a <em>copy</em> of the record we secretly changed one number
                ({String(tamper.tampered_copy.before)} → {String(tamper.tampered_copy.after)}). Its
                {" "}<Term k="Digital signature">seal</Term> broke at line {tamper.tampered_copy.entry_index}.
              </p>
            </div>
          </div>
          {tamper.truncated_copy && (
            <div className={`cheat-row ${tamper.truncation_detected ? "caught" : "missed"}`}>
              <span className="cheat-icon">{tamper.truncation_detected ? "🚨" : "✗"}</span>
              <div>
                <strong>{tamper.truncation_detected ? "Caught! Hiding the ending is detected too." : "Not detected"}</strong>
                <p className="small">
                  We deleted the last {tamper.truncated_copy.dropped_entries} lines of a copy (for example, to hide how
                  the run ended). Every remaining seal is still fine, but the record no longer ends with its sealed
                  closing line, so it is marked <strong>incomplete</strong>.
                </p>
              </div>
            </div>
          )}
          <p className="muted small">The real record was never touched. Only in-memory copies were changed.</p>
        </div>
      )}

      {report && (
        <div className="proof">
          {a?.correctness_detail && (
            <div className={`answer ${a.verdict === "VERIFIED" ? "good" : "bad"}`}>
              <h4>Is the agent's answer right?</h4>
              <table className="answer-table">
                <thead><tr><th></th><th>The agent said</th><th>AgentLimit re-calculated</th></tr></thead>
                <tbody>
                  {Object.keys(a.correctness_detail.expected).map((k) => {
                    const ok = a.correctness_detail!.expected[k] === a.correctness_detail!.actual[k];
                    return (
                      <tr key={k}><td>{k.replace(/_/g, " ")}</td><td>{String(a.correctness_detail!.actual[k])}</td>
                        <td>{String(a.correctness_detail!.expected[k])} {ok ? "✓" : "✗"}</td></tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="small">
                {a.verdict === "VERIFIED"
                  ? "✓ Correct. AgentLimit didn't trust the agent: it re-did the maths itself from the original sealed files."
                  : "✗ The agent's answer does not match the independent calculation."}
              </p>
            </div>
          )}
          <h4>Proof checklist</h4>
          <ul className="proof-list">
            {Object.entries(report.verification).map(([k, v]) => (
              <li key={k} className={VCLS[v] ?? ""}>
                <span className="proof-verdict">{VERDICT_PLAIN[v] ?? v}</span>
                <span>{VERIFY_PLAIN[k] ?? k}</span>
                {technical && <span className="tech-line">{AREA[k] ?? k}: {v}</span>}
              </li>
            ))}
          </ul>
          <p className="small">
            <Term k="Fingerprint">Seal fingerprint</Term>: <code>{report.public_key_fingerprint}</code>{" "}
            {report.public_key_fingerprint === PUBLISHED_FINGERPRINT ? (
              <span className="ok">✓ matches the one published in the README, so these seals are genuinely AgentLimit's.</span>
            ) : (
              <span className="warn">does not match the published fingerprint {PUBLISHED_FINGERPRINT} (different server or key).</span>
            )}
          </p>
        </div>
      )}

{technical && tamper && (
        <div className={`tamper ${tamper.tamper_detected ? "ok" : "fail"}`}>
          <strong>{tamper.tamper_detected ? "✓ Tampering detected" : "✗ Tampering NOT detected"}</strong>
          <p className="small">
            Changed {tamper.tampered_copy.field} at entry {tamper.tampered_copy.entry_index}:{" "}
            {String(tamper.tampered_copy.before)} → {String(tamper.tampered_copy.after)} in a copy. Verifier:{" "}
            {tamper.tampered_copy.detail}. Original ledger: {tamper.original.valid ? "valid" : "INVALID"}.
          </p>
          {tamper.truncated_copy && (
            <p className="small">
              <strong className={tamper.truncation_detected ? "ok" : "fail"}>
                {tamper.truncation_detected ? "✓ Truncation detected" : "✗ Truncation NOT detected"}
              </strong>{" "}
              — dropped the last {tamper.truncated_copy.dropped_entries} entries of a copy: hash chain still{" "}
              {tamper.truncated_copy.integrity_valid ? "valid" : "invalid"}, but completeness ={" "}
              <strong>{tamper.truncated_copy.completeness}</strong> ({tamper.truncated_copy.detail}).
            </p>
          )}
          <p className="muted small">{tamper.note}</p>
        </div>
      )}

      {technical && report && (
        <div className="evidence-grid">
          <div>
            <h4>What the agent claimed vs what was verified</h4>
            <table className="claims">
              <thead>
                <tr><th></th><th>Agent's output</th><th>Independent recomputation</th></tr>
              </thead>
              <tbody>
                {a?.correctness_detail ? (
                  Object.keys(a.correctness_detail.expected).map((k) => (
                    <tr key={k} className={a.correctness_detail!.expected[k] === a.correctness_detail!.actual[k] ? "ok" : "fail"}>
                      <td>{k}</td>
                      <td>{String(a.correctness_detail!.actual[k])}</td>
                      <td>{String(a.correctness_detail!.expected[k])}</td>
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
            {a?.integrity_detail && (
              <p className="muted small">
                output sha256 recorded in ledger {a.integrity_detail.recorded_in_ledger?.slice(0, 16)}… · file on disk{" "}
                {a.integrity_detail.actual?.slice(0, 16)}… · {a.integrity_detail.match ? "match" : "MISMATCH"}
              </p>
            )}
            <p className="muted small">
              inputs re-hashed against the signed manifest: {report.inputs.verdict} ({Object.keys(report.inputs.files).join(", ")})
            </p>
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
            <h4>Verification by area (each re-derived from the signed ledger)</h4>
            <table className="claims">
              <tbody>
                {Object.entries(report.verification).map(([k, v]) => (
                  <tr key={k}>
                    <td>{AREA[k] ?? k}</td>
                    <td className={VCLS[v] ?? ""}><strong>{v}</strong></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {report.containment.failed_checks?.length > 0 && (
              <p className="fail small">containment failed: {report.containment.failed_checks.join(", ")}</p>
            )}
            {report.inference_verification?.accounting === "PARTIAL" && (
              <p className="warn small">
                {report.inference_verification.uncertain_reserved_tokens} tokens reserved with no provider usage
                received — counted as uncertain spend, not as measured.
              </p>
            )}
          </div>

          <div>
            <h4>Session</h4>
            <p className="small">
              final state <span className={`state-pill state-${report.final_state.toLowerCase()}`}>{report.final_state}</span> · agent{" "}
              {report.agent_status} · {report.steps_completed} steps · {report.inference.total_tokens.toLocaleString()} tokens ·
              est. ${(report.inference.estimated_cost_usd ?? 0).toFixed(5)} · {report.entry_count} signed entries
            </p>
            {!report.session_started && (
              <p className="fail small">session refused at start: {report.start_failure?.category}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
