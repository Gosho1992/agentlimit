import type { GovernanceState } from "../types";
import { LADDER, STATES, type StateInfo } from "../plain";

/** Big traffic-light banner: one colour, one icon, one sentence. */
export default function StatusBanner({ info, state }: { info: StateInfo; state: GovernanceState }) {
  const cur = LADDER.indexOf(state);
  return (
    <div id="tour-status" className={`banner sig-${info.signal}`}>
      <div className="banner-main">
        <span className="banner-icon" aria-hidden>{info.icon}</span>
        <div>
          <div className="banner-label">
            {info.label} {info.tech && <span className="banner-tech">{info.tech}</span>}
          </div>
          <div className="banner-say">{info.say}</div>
        </div>
      </div>
      <div id="tour-ladder" className="lights" aria-label="Safety ladder">
        {LADDER.map((s, i) => {
          const st = STATES[s];
          return (
            <div key={s} className={`light sig-${st.signal} ${i === cur ? "on" : i < cur ? "past" : ""}`} title={st.say}>
              <span className="light-dot" />
              <span className="light-name">{st.label}</span>
              <span className="light-tech">{st.tech}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
