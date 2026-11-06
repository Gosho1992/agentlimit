import type { Recommendation } from "../types";

const SEVERITY_COLOR: Record<string, string> = { info: "#3b82f6", warning: "#f97316", critical: "#ef4444" };

export default function RecommendationCard({ rec }: { rec: Recommendation }) {
  const color = SEVERITY_COLOR[rec.severity] || "#64748b";
  return (
    <div className="rec-card" style={{ borderLeftColor: color }}>
      <div className="rec-header">
        <span className="rec-rule">{rec.rule_id}</span>
        <span className="rec-severity" style={{ color }}>
          {rec.severity} · {rec.status}
        </span>
      </div>
      <p className="rec-suggestion">{rec.suggestion}</p>
      <div className="rec-meta">
        <span>
          observed {rec.metric} = {rec.observed} {rec.unit} (threshold {rec.threshold})
        </span>
        <span>action: {rec.action}{rec.auto_applicable ? " (auto)" : " (needs operator)"}</span>
      </div>
      {rec.risk && <p className="muted small">trade-off: {rec.risk}</p>}
    </div>
  );
}
