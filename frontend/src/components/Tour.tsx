import { useEffect, useState } from "react";

const STEPS = [
  { id: "tour-status", title: "1. The traffic light", text: "One colour tells you everything. Green = all good, yellow = warning, orange = slowed down, blue = frozen, red = stopped for good." },
  { id: "tour-ladder", title: "2. The safety ladder", text: "AgentLimit doesn't jump straight to killing. It steps up one level at a time, like a referee: warning, advice, slow down, freeze, stop." },
  { id: "tour-meters", title: "3. The limits", text: "Before the run you set limits (a budget). AgentLimit measures them from outside the sandbox, so the agent can't lie about how much it used." },
  { id: "tour-story", title: "4. What's happening", text: "Each step in plain words, newest first. Every line comes from a signed record that can't be secretly edited." },
  { id: "tour-controls", title: "5. You're in control", text: "Freeze the agent or stop it for good at any moment. Stop for good also destroys its sandbox and cuts its AI access." },
  { id: "tour-proof", title: "6. The proof", text: "When the run ends, AgentLimit checks everything again: was the answer right, was the sandbox sealed, did the stop really happen?" },
  { id: "tour-tamper", title: "7. Try to cheat", text: "Press this to secretly change a copy of the record. AgentLimit catches it instantly. That's why you can trust the proof." },
];

const KEY = "agentlimit-tour-done";

export function tourSeen(): boolean {
  try { return localStorage.getItem(KEY) === "1"; } catch { return false; }
}

export default function Tour({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [i, setI] = useState(0);
  const steps = STEPS.filter((s) => document.getElementById(s.id));
  const step = steps[Math.min(i, steps.length - 1)];

  useEffect(() => { if (open) setI(0); }, [open]);
  useEffect(() => {
    if (!open || !step) return;
    const el = document.getElementById(step.id);
    el?.classList.add("tour-focus");
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    return () => el?.classList.remove("tour-focus");
  }, [open, step?.id]);

  if (!open || !step) return null;
  const close = () => {
    try { localStorage.setItem(KEY, "1"); } catch { /* storage unavailable */ }
    onClose();
  };
  const last = i >= steps.length - 1;
  return (
    <div className="tour-card" role="dialog" aria-label="Guided tour">
      <div className="tour-count">{Math.min(i, steps.length - 1) + 1} / {steps.length}</div>
      <h4>{step.title}</h4>
      <p>{step.text}</p>
      <div>
        <button onClick={() => (last ? close() : setI(i + 1))}>{last ? "Got it" : "Next →"}</button>
        {!last && <button className="btn-ghost" onClick={close}>Skip</button>}
      </div>
    </div>
  );
}
