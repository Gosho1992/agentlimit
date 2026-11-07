import type { LedgerEntry } from "../types";
import { narrate } from "../plain";

/** The run told as a short story, newest first, straight from the signed record. */
export default function Story({ entries, technical }: { entries: LedgerEntry[]; technical: boolean }) {
  const seen = new Set<string>();
  const rows = entries
    .filter((e) => !seen.has(e.entry_hash) && (seen.add(e.entry_hash), true))
    .map((e) => ({ e, n: narrate(e) }))
    .filter((r) => r.n) as { e: LedgerEntry; n: { text: string; signal?: string } }[];
  const latest = rows[rows.length - 1];
  return (
    <div id="tour-story" className="story">
      <h2 className="card-title">What's happening</h2>
      {latest ? (
        <p className={`story-now sig-text-${latest.n.signal ?? "grey"}`}>{latest.n.text}</p>
      ) : (
        <p className="muted">Getting ready…</p>
      )}
      <ol className="story-list" tabIndex={0} aria-label="Timeline of this run, newest first">
        {rows.slice().reverse().map(({ e, n }) => (
          <li key={e.entry_hash}>
            <span className={`story-dot sig-${n.signal ?? "grey"}`} />
            <span className="story-time">{new Date(e.timestamp_utc).toLocaleTimeString()}</span>
            <span className="story-text">
              {n.text}
              {technical && <span className="tech-line">{e.event_type} · {JSON.stringify(e.payload).slice(0, 160)}</span>}
            </span>
          </li>
        ))}
      </ol>
      <p className="muted small">Every line comes from AgentLimit's signed record, not from the agent.</p>
    </div>
  );
}
