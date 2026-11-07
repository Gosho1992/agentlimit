import Term from "./Term";

/** A limit meter with plain wording and traffic-light colours. */
export default function Meter({ name, term, used, limit, fmt, hint }: {
  name: string; term?: string; used: number; limit: number; fmt: (n: number) => string; hint?: string;
}) {
  const pct = Math.min(100, limit > 0 ? (used / limit) * 100 : 0);
  const sig = pct >= 100 ? "red" : pct >= 95 ? "blue" : pct >= 85 ? "orange" : pct >= 70 ? "yellow" : "green";
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{term ? <Term k={term}>{name}</Term> : name}</span>
        <span className="meter-val">{fmt(used)} of {fmt(limit)}</span>
      </div>
      <div className="meter-track">
        <div className={`meter-fill sig-${sig}`} style={{ width: `${pct}%` }} />
        <span className="meter-mark" style={{ left: "70%" }} title="Heads up at 70%" />
        <span className="meter-mark" style={{ left: "85%" }} title="Slowed down at 85%" />
        <span className="meter-mark" style={{ left: "95%" }} title="Frozen at 95%" />
      </div>
      <div className="meter-foot"><span className="muted small">{hint}</span><span className="small">{pct.toFixed(0)}%</span></div>
    </div>
  );
}
