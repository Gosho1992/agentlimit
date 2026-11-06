interface Props {
  label: string;
  used: number;
  limit: number;
  unit: string;
  format?: (n: number) => string;
}

export default function ResourceGauge({ label, used, limit, unit, format }: Props) {
  const pct = Math.min(100, limit > 0 ? (used / limit) * 100 : 0);
  const color =
    pct >= 100 ? "#ef4444" : pct >= 85 ? "#f97316" : pct >= 70 ? "#eab308" : "#22c55e";
  const fmt = format || ((n: number) => `${n.toFixed(1)} ${unit}`);

  return (
    <div className="gauge">
      <div className="gauge-header">
        <span className="gauge-label">{label}</span>
        <span className="gauge-value">
          {fmt(used)} / {fmt(limit)}
        </span>
      </div>
      <div className="gauge-track">
        <div
          className="gauge-fill"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
      <div className="gauge-pct">{pct.toFixed(1)}%</div>
    </div>
  );
}